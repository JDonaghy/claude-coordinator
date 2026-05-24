//! Backend-neutral app logic for coord-tui.
//!
//! [`CoordApp`] implements [`quadraui::ShellApp`] using only the
//! backend-neutral trait surface (`draw_list`, `draw_split`, `draw_tree`,
//! `draw_status_bar`). No ratatui or crossterm symbols appear here —
//! those live exclusively in the TUI and GTK shim entry points.
//!
//! ## Layout
//!
//! ```text
//! ┌──┬──────────────────────────────┬──────────────────────────────────┐
//! │B │ Board tree / Machines list   │ DETAIL                           │
//! │M │ ▼ Running (1)                │ claude-coordinator #115          │
//! │P │   #115  claude-coord  RUN    │  ID     6b2670e…                 │
//! │  │ ▼ Failed (0)                 │  Machine dellserver              │
//! │  │ ▶ Done (3)                   │                                  │
//! │  │                              │                                  │
//! ├──┴──────────────────────────────┴──────────────────────────────────┤
//! │ coord-tui  Board  ↻ 3s  1=Board 2=Machines 3=Pipeline  j/k  q     │
//! └────────────────────────────────────────────────────────────────────┘
//!  ↑ activity bar    ↑ sidebar (35 cols)    ↑ main content
//! ```
//!
//! The leftmost column is the quadraui AppShell activity bar (B/M/P icons).
//! The shell handles activity bar clicks, sidebar toggle, and divider drag.
//! `render_content()` draws the list (tree/machines/pipeline) into
//! `sidebar_content_bounds`, the detail panel into `main_content_bounds`,
//! and the status bar into `status_bar_bounds`.
//!
//! **Data sources:**
//! - `~/.coord/coord.db` — SQLite database (WAL mode) written by the coordinator
//!
//! **Auto-refresh:** every 5 s, checked at the start of each [`ShellApp::handle`]
//! call (the [`ShellApp`] trait has no `tick()` callback).

use std::net::{TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use rusqlite::{Connection, OpenFlags};

use quadraui::compose::app_shell::{AppShellEvent, AppShellLayout, PanelDefinition};
use quadraui::compose::form_controller::{FormController, FormControllerEvent};
use quadraui::compose::sidebar_system::{
    NavigationMode, SidebarEvent, SidebarSectionDef, SidebarSystem,
};
use quadraui::primitives::context_menu::{
    ContextMenu, ContextMenuHit, ContextMenuItem as QuiContextMenuItem,
    ContextMenuItemMeasure, ContextMenuLayout, ContextMenuPlacement,
};
use quadraui::primitives::form::{FieldKind, Form, FormEvent, FormField};
use quadraui::primitives::toast::{ToastCorner, ToastItem, ToastSeverity, ToastStack};

use crate::settings::{LogCacheTtl, ModelPref, RefreshCadence, Theme, TuiSettings};
use quadraui::{
    Backend, Badge, Color, Decoration, Key, ListItem, ListView, MouseButton, NamedKey,
    PipelineHit, PipelineStage as QuiPipelineStage, PipelineView as QuiPipelineView,
    Point, Reaction, Rect, ScrollDelta, ScrollMode, SectionSize, ShellApp,
    ShellConfig, ShellContext, SidebarPanel, SidebarPanelHit, StageStatus, StatusBar,
    StatusBarSegment, StyledSpan, StyledText, TabBar, TabItem, Toolbar, ToolbarButton,
    ToolbarHoverTracker, ToolbarItemMeasure, TreeRow, UiEvent, WidgetId,
};

// ─── Auto-refresh interval ────────────────────────────────────────────────────

/// Auto-run `coord notify` every 30 seconds when assignments are running.
const NOTIFY_EVERY: Duration = Duration::from_secs(30);

/// How long a toast stays visible before auto-dismissing.
const TOAST_TTL: Duration = Duration::from_secs(4);

// ─── Detail panel tabs ────────────────────────────────────────────────────────

/// Per-assignment context for the live watch overlay (Pipeline > Stages
/// > Enter).  The overlay takes over the main panel, auto-refreshes the
/// worker log every render, and accepts K/q/scroll keys.
#[derive(Clone)]
struct WatchState {
    assignment_id: String,
    machine: String,
    repo: String,
    issue_number: u64,
    /// Assignment type ("work", "plan", "review"). Drives which control
    /// keys are available — e.g. 'A' (approve plan → dispatch work) is
    /// only meaningful when watching a plan-type assignment.
    assignment_type: String,
    /// Scroll offset into the log lines.  `usize::MAX` is a sentinel
    /// meaning "stick to the bottom"; any explicit user scroll replaces
    /// this with a concrete row.
    scroll: usize,
}

/// Messages sent from the background SSE watch thread to the main thread.
enum SseWatchMsg {
    /// New log text arrived; `last_id` is the byte-offset after this chunk
    /// (used as `Last-Event-Id` on reconnect to resume without refetching).
    Lines { last_id: u64, text: String },
    /// Stream ended cleanly (agent sent `event: end`). No reconnect needed.
    Done { last_id: u64 },
    /// Connection or read error. The main thread decides whether to reconnect.
    Error(String),
    /// SSE keepalive comment received. Used to detect when the receiver has
    /// been dropped (cancel signal): if `tx.send` fails, the thread exits.
    Heartbeat,
}

/// State for the live SSE log-stream connection backing the watch overlay.
///
/// Held on `CoordApp` separately from `WatchState` because `Receiver<T>`
/// is not `Clone` and `WatchState` must be.  Dropped (and thus the background
/// thread cancelled) when the overlay closes via `close_watch()`.
struct WatchSseState {
    /// Receive end of the channel from the background SSE thread.
    rx: std::sync::mpsc::Receiver<SseWatchMsg>,
    /// Accumulated raw log lines, appended as `Lines` messages arrive.
    lines: Vec<String>,
    /// Byte offset of the last received event, for `Last-Event-Id` on reconnect.
    last_event_id: u64,
    /// Number of connection failures in the current 10-second window.
    fail_count: u32,
    /// When the first failure in the current window occurred, for TTL reset.
    first_fail_at: Option<Instant>,
    /// True once a clean `end` event arrives or the failure limit is hit.
    /// When true, no further reconnect attempts are made.
    done: bool,
    /// Machine hostname, stored here so reconnect doesn't need `self.watch`.
    host: String,
    /// Assignment ID, stored here so reconnect doesn't need `self.watch`.
    assignment_id: String,
    /// Partial trailing line carried over between SSE chunks. The agent reads
    /// the log in fixed 4 KB chunks (events.LOG_CHUNK_SIZE), so a long JSON
    /// line (e.g. a `{"type":"result"...}` event with the full review body)
    /// can be split mid-line. Without reassembly the client would parse two
    /// broken halves and lose `total_cost_usd` / `stop_reason` from the
    /// metrics line. Held here until the next chunk arrives.
    pending_tail: String,
}

/// #235 Phase 1: in-flight `coord test <work_id>` build job spawned from
/// the TUI's local machine. Re-uses the existing CLI which does git fetch +
/// checkout + `repo.build_command`, so no Rust-side git/build logic is
/// duplicated here. Job state lives in-memory only — restarting the TUI
/// drops it; the user can re-press `B` to retrigger.
struct TestBuildJob {
    /// Work assignment id this build is verifying. Also the HashMap key.
    #[allow(dead_code)]
    work_id: String,
    /// Issue number, for friendlier toast titles (work_ids are uuids).
    issue_number: u64,
    /// Branch passed to `coord test` (carried for the completion toast).
    branch: String,
    /// `~/.coord/test-build-<id>.log` — stdout+stderr from the subprocess.
    /// Surfaced in failure toasts so the user can `tail` it.
    log_path: PathBuf,
    /// Wall-clock start; reported in the success toast as "took Ns".
    started_at: Instant,
    /// Receiver for the completion message; `try_recv` polled each tick.
    rx: std::sync::mpsc::Receiver<TestBuildOutcome>,
}

/// Outcome of a Phase 1 build. Sent once from the worker thread.
struct TestBuildOutcome {
    exit_code: i32,
    /// First non-empty stderr line (truncated). Surfaced in the failure
    /// toast so the user doesn't have to `cat` the log to see the cause.
    /// Empty string on success, since we don't bother capturing.
    first_error: String,
}

/// #271 part 2: persisted record of the most-recent Phase 1 build for
/// a work assignment.  Survives long after the 4-second toast expires,
/// so the user who walked away during a 5-minute `cargo build` returns
/// to a visible "Last build: ✓ 2m 30s" or "✗ exit 101: <first error>"
/// line in the Pipeline detail panel.
///
/// Stored on `CoordApp.last_test_builds` keyed by `work_id`.  A new
/// `spawn_test_build` for the same work id replaces the prior entry.
#[derive(Clone)]
struct TestBuildResult {
    branch: String,
    issue_number: u64,
    /// Exit code from `coord test`.  0 = success.
    exit_code: i32,
    /// First non-empty stderr line — useful one-liner for the failure
    /// case.  Empty on success.
    first_error: String,
    log_path: PathBuf,
    /// Wall-clock duration of the build.
    duration_secs: u64,
    /// When the build finished, used by the renderer to format an
    /// "Xs ago" hint.
    finished_at: Instant,
}

/// The tabs shown in the Pipeline view detail panel.
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
enum PipelineDetailTab {
    /// Horizontal stage view + repo/labels/gates meta.
    #[default]
    Pipeline,
    /// Full issue body text (scrollable with j/k).
    Issue,
    /// Per-stage detail: assignment id, machine, status, timing,
    /// exit code (or merge-queue state for the merge stage).
    Stages,
}

/// The tabs shown in the Board view detail panel.
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
enum BoardDetailTab {
    /// Default: assignment summary, status, machine, etc.
    #[default]
    Board,
    /// Full issue body text + labels (scrollable with j/k or scrollwheel).
    /// Reuses `issue_body_list` so the rendering matches the Pipeline view.
    Issue,
}

// ─── Sidebar views ────────────────────────────────────────────────────────────

/// The selectable top-level views shown in the left sidebar.
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
enum SidebarView {
    #[default]
    Board,
    Machines,
    /// Pipeline panel: tracked-issue list + horizontal stage view per issue.
    Pipeline,
    /// Settings panel: category nav on the left, form controls on the right.
    Settings,
}

impl SidebarView {
    fn label(self) -> &'static str {
        match self {
            SidebarView::Board => "Board",
            SidebarView::Machines => "Machines",
            SidebarView::Pipeline => "Pipeline",
            SidebarView::Settings => "Settings",
        }
    }
}

// ─── App data model ───────────────────────────────────────────────────────────

#[derive(Clone)]
struct Assignment {
    id: String,
    repo: String,
    issue_number: u64,
    issue_title: String,
    machine: String,
    status: String,
    branch: Option<String>,
    model: Option<String>,
    dispatched_at: Option<f64>,
    finished_at: Option<f64>,
    exit_code: Option<i32>,
    assignment_type: Option<String>,
    /// #200: human-driven Test gate verdict for type="work" assignments.
    /// None | "passed" | "failed" | "skipped".
    test_state: Option<String>,
    /// #253: parsed adversarial-review verdict for type="review" assignments.
    /// None | "approve" | "request-changes".  Drives the merge-gate hint
    /// swap so the user sees the block before pressing m.
    review_verdict: Option<String>,
    /// #253: links a review assignment back to the work assignment it
    /// reviews — needed to pair review_verdict with the merge entry.
    review_of_assignment_id: Option<String>,
    /// #208: worker cost captured from the final stream-json result event.
    /// `None` for in-flight workers and for pre-#208 rows.
    cost_usd: Option<f64>,
    /// #252: worker-emitted smoke-test list, parsed from the SMOKE_TESTS
    /// block in the worker's log.
    ///
    /// * `None`     — no block found (graceful degradation: TUI shows
    ///                "inspect the diff" placeholder).
    /// * `Some([])` — explicit "(none — change is internal)" form.
    /// * `Some(vec)` — bullets to render under the Test stage.
    smoke_tests: Option<Vec<String>>,
    /// #bounce: cached review findings (verdict + body), JSON-encoded
    /// in the DB column.  `None` for non-review assignments and for
    /// reviews completed before the cache landed.
    review_findings: Option<String>,
}

impl Assignment {
    fn status_color(&self) -> Color {
        match self.status.as_str() {
            "running" => Color::rgb(80, 220, 80),
            "done" => Color::rgb(120, 120, 120),
            "failed" => Color::rgb(220, 70, 70),
            _ => Color::rgb(200, 200, 70),
        }
    }

    fn status_label(&self) -> &str {
        match self.status.as_str() {
            "running" => "RUN ",
            "done" => "DONE",
            "failed" => "FAIL",
            _ => "PEND",
        }
    }

    fn age_str(&self) -> String {
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
struct Machine {
    name: String,
    /// Tailscale FQDN (the `host` column in the machines table).
    host: String,
    reachable: bool,
    active_count: usize,
    repos: Vec<String>,
}

/// An issue grouped with all its assignments and a summary status.
#[derive(Clone)]
struct IssueGroup {
    issue_number: u64,
    issue_title: String,
    assignments: Vec<Assignment>,
    /// Derived summary: "running", "failed", "done", "merged", "pending"
    status_summary: String,
    /// #265: whether the underlying GitHub issue is closed.  Populated
    /// from `data.open_issues` (which carries both `open` and `closed`
    /// states).  Drives the In-flight vs Completed section split.
    /// Defaults to false when the issue isn't in the cache.
    is_closed: bool,
    /// #265 / #257 fix: whether the brain has the issue as currently
    /// `state="open"` in the local cache.  Distinguishes "open and
    /// known active" from "not in cache at all" (the latter typically
    /// means the issue was closed long enough ago that the brain has
    /// pruned its row — see `coord/state.py::upsert_open_issues`,
    /// which only retains closed rows for 7 days).
    ///
    /// Without this distinction, every historical merged/done
    /// assignment on a long-closed issue routes to In-flight, swamping
    /// the lifecycle ledger with ancient work.
    has_open_record: bool,
    /// #226: GitHub labels (e.g. `status:refining`, `status:ready`,
    /// `coord`).  Populated from `data.open_issues`.  Empty for issues
    /// that aren't in the cache.  Drives the Backlog / Refining /
    /// Refined split inside the Pending bucket.
    labels: Vec<String>,
}

impl IssueGroup {
    fn status_color(&self) -> Color {
        match self.status_summary.as_str() {
            "running" => Color::rgb(80, 220, 80),
            "failed" => Color::rgb(220, 70, 70),
            "done" => Color::rgb(120, 180, 120),
            "merged" => Color::rgb(100, 180, 240),
            _ => Color::rgb(140, 140, 160),
        }
    }

    /// #256 / #226 lifecycle bucketing.  Returns the section key the
    /// row belongs in:
    /// - `"backlog"` — no assignments, no `status:*` label
    /// - `"refining"` — no assignments, `status:refining` label
    /// - `"refined"` — no assignments, `status:ready` label
    /// - `"in-flight"` — has at least one open assignment
    /// - `"completed"` — closed issue + assignments (or stale settled
    ///   assignment with no open cache record)
    ///
    /// Rules (in order):
    /// - No assignments → split into Backlog/Refining/Refined by label.
    /// - Issue cache says closed → **Completed**.
    /// - Issue is `merged` (PR closed the issue via `fixes #N`) →
    ///   **Completed**, even when the brain hasn't synced yet.
    /// - Issue has no open-cache record AND assignment is settled
    ///   (`done`/`merged`) → **Completed**.
    /// - Otherwise (with assignments) → **In-flight**.
    fn lifecycle_section(&self) -> &'static str {
        if self.assignments.is_empty() {
            // #226: split Pending by `status:*` label.
            if self.labels.iter().any(|l| l == "status:refining") {
                return "refining";
            }
            if self.labels.iter().any(|l| l == "status:ready") {
                return "refined";
            }
            return "backlog";
        }
        if self.is_closed {
            return "completed";
        }
        if self.status_summary == "merged" {
            return "completed";
        }
        if !self.has_open_record
            && matches!(self.status_summary.as_str(), "done" | "merged")
        {
            return "completed";
        }
        "in-flight"
    }
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
enum ContextMenuTarget {
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
}

/// #262: lifecycle bucket for a Pipeline sidebar row at right-click
/// time.  Mirrors the umbrella's three Pipeline sections (New /
/// In-progress / Done) plus a catch-all for the existing classifier's
/// "refining" / "new" rows (coord-labelled but not dispatch-ready),
/// where Start is not yet appropriate.
#[derive(Clone, Debug, PartialEq, Eq)]
enum PipelineRowLifecycle {
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
enum BoardRowLifecycle {
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
enum PipelineMergeState {
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
    Ready { issue: u64, repo: String },
}

/// #259: one item in an open context menu.  Lightweight engine-side
/// shape converted to `quadraui::ContextMenuItem` at render time.
#[derive(Clone, Debug)]
struct ContextMenuItem {
    /// Action identifier dispatched on click / Enter.  `None` ⇒ separator.
    action_id: Option<String>,
    label: String,
    /// Optional right-aligned shortcut hint (e.g. `"r"`).
    shortcut: Option<String>,
    disabled: bool,
}

impl ContextMenuItem {
    fn action(id: &str, label: &str) -> Self {
        Self {
            action_id: Some(id.to_string()),
            label: label.to_string(),
            shortcut: None,
            disabled: false,
        }
    }
    fn separator() -> Self {
        Self {
            action_id: None,
            label: String::new(),
            shortcut: None,
            disabled: false,
        }
    }
    fn with_shortcut(mut self, s: &str) -> Self {
        self.shortcut = Some(s.to_string());
        self
    }
}

/// #259: state of an open right-click context menu.  `None` on
/// `CoordApp.pending_context_menu` means no menu is showing.
#[derive(Clone, Debug)]
struct ContextMenuState {
    items: Vec<ContextMenuItem>,
    /// Anchor point — where the right-click landed.
    anchor: Point,
    /// Keyboard-selected item index.  Maintained skipping separators by
    /// `quadraui::ContextMenu::move_selection`.
    selected_idx: usize,
    /// What the menu is "about" — used by `dispatch_context_menu_action`
    /// to route the action with row-specific data.
    target: ContextMenuTarget,
}

#[derive(Clone)]
#[allow(dead_code)] // assignment_id and pr_url stored for future display
struct MergeQueueEntry {
    assignment_id: String,
    issue_number: Option<u64>,
    state: String,
    pr_number: Option<i64>,
    pr_url: Option<String>,
    /// Repo slug (owner/name) — needed to call `gh pr checks --repo <slug>`.
    /// Joined from the `merge_queue.repo_github` column.
    repo_github: String,
}

/// CI check status for one PR, fetched in the background via `gh pr checks`.
///
/// Populated from `fetch_ci_checks_summary` and stored on `CoordApp` keyed by
/// `(repo_github, pr_number)`. Drives the "Checks: 2✓ 1✗" line under the
/// Merge stage in the Pipeline detail tab and the "Checks failed" status bar
/// hint when Merge is actionable.
#[derive(Clone, Debug)]
struct CiCheckSummary {
    passed: usize,
    failed: usize,
    running: usize,
    /// Names of failed checks (for the status-bar hint and detail row).
    failed_names: Vec<String>,
    /// URL of the first failed check — surfaced as a clickable link target
    /// in the detail row. We only show one in the terse summary; the user
    /// can press Enter to open the PR for the full list.
    first_failed_url: Option<String>,
    /// When this summary was fetched. Used to TTL the cache.
    fetched_at: Instant,
}

impl CiCheckSummary {
    fn has_failures(&self) -> bool {
        self.failed > 0
    }

    /// One-line summary like `2✓ 1✗ 1⋯`. Empty string when no checks at all
    /// (caller can suppress the row in that case).
    fn terse(&self) -> String {
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

#[derive(Clone)]
struct Proposal {
    id: i64,
    machine: String,
    repo: String,
    issue_number: u64,
    issue_title: String,
    rationale: String,
    proposal_type: String,
}

/// One GitHub issue tracked by the pipeline panel.
///
/// Sourced from a background `gh search issues label:<L> --state all` poll
/// and matched back to a coord-local repo name via `pipeline_repos` in
/// `board_meta`.
#[derive(Clone, Debug, PartialEq, Eq)]
struct PipelineIssue {
    /// Issue number within the GitHub repo.
    number: u64,
    /// Issue title (as returned by gh).
    title: String,
    /// Issue body text (as returned by gh). Empty string when absent.
    body: String,
    /// `owner/name` slug of the GitHub repo the issue lives in.
    repo_slug: String,
    /// Coord-local repo name (matched via `pipeline_repos` map). `None` when
    /// the issue is in a repo not declared in coordinator.yml — such issues
    /// are still listed but cannot be dispatched.
    coord_repo: Option<String>,
    /// Tracked labels that flagged this issue (subset of `all_labels`).
    matched_labels: Vec<String>,
    /// All GitHub labels on this issue (not filtered by tracked labels).
    /// Used to compute lifecycle sections (status:refining, status:ready, …).
    all_labels: Vec<String>,
    /// True when the issue is closed on GitHub (`state == "closed"`).
    is_closed: bool,
}

#[derive(Default)]
/// A single issue freshly fetched via `gh issue view` for the Board Issue tab
/// when no row exists in the local `issues` table. Mirrors [`OpenIssue`] but
/// produced on-demand rather than from a sync.
#[derive(Clone, Debug)]
struct FetchedIssue {
    number: u64,
    title: String,
    body: String,
    labels: Vec<String>,
    /// "open" | "closed".  Carried so the DB upsert mirrors what `coord sync`
    /// would have written.
    state: String,
}

/// #271 part 2 follow-up: cached `gh pr view` snapshot for a Pipeline
/// PR.  Surfaces the worker's PR description (which typically explains
/// what they did, including any new sample apps / demo binaries / entry
/// points) plus the list of files touched and the latest review's
/// verdict + body, so the user testing the branch has in-TUI context
/// instead of having to ask Claude or click out to GitHub.
#[derive(Clone, Debug)]
struct FetchedPr {
    title: String,
    /// Markdown body of the PR.  May be empty when the worker / merge
    /// command didn't write one.
    body: String,
    /// File paths touched by the PR.  Sorted by gh's default ordering.
    files: Vec<String>,
    /// Reviews posted on this PR, in the order gh returns them
    /// (chronological).  The latest one is what the user typically
    /// cares about (was it an approve or request-changes?).  When the
    /// adversarial reviewer posted via `coord notify`, the body is the
    /// full review text the reviewer wrote.
    reviews: Vec<FetchedReview>,
}

/// One row from `gh pr view --json reviews`.  `state` is gh's verbatim
/// status string: `"APPROVED"`, `"CHANGES_REQUESTED"`, `"COMMENTED"`,
/// or `"PENDING"`.
#[derive(Clone, Debug)]
struct FetchedReview {
    /// gh status string (uppercase).
    state: String,
    /// Markdown review body.  Empty when the reviewer left only a
    /// status change with no comment.
    body: String,
}

/// #248: parsed `<!-- coord:review ... -->` header.  The coordinator
/// prepends this to every review body so the TUI can surface the
/// verdict + counts without ingesting the prose.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct CoordReviewHeader {
    verdict: Option<String>,
    blocking: Option<u32>,
    nonblocking: Option<u32>,
    nits: Option<u32>,
    reviewer: Option<String>,
    assignment: Option<String>,
}

/// Extract the `<!-- coord:review ... -->` header from a review body.
/// Returns `None` when the header is missing or malformed.  Tolerates
/// extra whitespace and unknown tokens — only `verdict` is required.
fn parse_coord_review_header(body: &str) -> Option<CoordReviewHeader> {
    let start = body.find("<!--").and_then(|s| {
        // Find a `coord:review` token within the same comment.
        let rest = &body[s..];
        let end = rest.find("-->")?;
        let inside = &rest[4..end];
        let trimmed = inside.trim();
        if !trimmed.starts_with("coord:review") && !trimmed.starts_with("coord: review") {
            return None;
        }
        let body_after = trimmed.split_once("coord:review").map(|(_, b)| b)?;
        Some(body_after.trim().to_string())
    })?;

    let mut header = CoordReviewHeader::default();
    for token in start.split_whitespace() {
        let (k, v) = match token.split_once('=') {
            Some(pair) => pair,
            None => continue,
        };
        let k_lower = k.to_ascii_lowercase();
        match k_lower.as_str() {
            "verdict" => header.verdict = Some(v.to_string()),
            "blocking" => header.blocking = v.parse().ok(),
            "nonblocking" => header.nonblocking = v.parse().ok(),
            "nits" => header.nits = v.parse().ok(),
            "reviewer" => header.reviewer = Some(v.to_string()),
            "assignment" => header.assignment = Some(v.to_string()),
            _ => {}
        }
    }
    if header.verdict.is_some() {
        Some(header)
    } else {
        None
    }
}

/// An open issue from the local `issues` table (synced from GitHub on coord plan).
#[derive(Clone)]
struct OpenIssue {
    repo_name: String,
    number: u64,
    title: String,
    /// Issue body, synced from GitHub via `coord sync`.  Empty string when
    /// the issue has no description.
    body: String,
    /// GitHub labels on this issue. Used by the Board Issue tab to render the
    /// same context the Pipeline Issue tab shows.
    labels: Vec<String>,
    /// "open" | "closed".  We load both into `data.open_issues` so the Board
    /// Issue tab can display bodies for closed issues (e.g. in the Completed
    /// group), but only "open" entries get injected as Pending rows.
    state: String,
}

#[derive(Default)]
struct BoardData {
    local_machine: String,
    assignments: Vec<Assignment>,
    /// Open issues from the local SQLite `issues` table — the full backlog.
    open_issues: Vec<OpenIssue>,
    machines: Vec<Machine>,
    merge_queue: Vec<MergeQueueEntry>,
    proposals: Vec<Proposal>,
    /// Pipeline gate names from `pipeline.default_gates` in coordinator.yml.
    /// Defaults to `["review", "merge"]` when the board_meta key is absent.
    pipeline_default_gates: Vec<String>,
    /// GitHub issue labels considered "in the pipeline". Defaults to
    /// `["coord"]` when the board_meta key is absent.
    pipeline_tracked_labels: Vec<String>,
    /// Coord-local repo name → GitHub `owner/repo` slug (and inverse).
    /// Empty when no config snapshot has been written yet.
    pipeline_repos: Vec<(String, String)>,
    /// Mirror of `dispatch.require_plan` from coordinator.yml.  When true,
    /// the pipeline prepends a Plan stage before Work, and Work [Go]
    /// becomes "approve the plan and dispatch work" rather than fresh
    /// dispatch.  Defaults to `false`.
    pipeline_require_plan: bool,
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

fn fmt_dur(secs: u64) -> String {
    if secs < 60 {
        format!("{}s", secs)
    } else if secs < 3600 {
        format!("{}m", secs / 60)
    } else {
        format!("{}h{}m", secs / 3600, (secs % 3600) / 60)
    }
}

/// Truncate `s` to at most `max_chars` Unicode scalar values.
/// Capitalize the first ASCII character of `s` (no-op when `s` is empty
/// or starts with a non-ASCII character).
fn capitalize(s: &str) -> String {
    let mut out = s.to_string();
    if let Some(c) = out.get_mut(0..1) {
        c.make_ascii_uppercase();
    }
    out
}

/// Format a unix timestamp as a relative "Xs/m/h ago" string using
/// the existing `fmt_dur` helper.  Falls back to "-" when the
/// timestamp is in the future or the system clock can't be read.
fn format_unix_time(ts: f64) -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    let delta = (now - ts).max(0.0) as u64;
    format!("{} ago", fmt_dur(delta))
}

/// #208: format a worker cost in USD with two decimals.  Below 1¢ shows
/// "< $0.01" so the rendering doesn't read as $0.00 (mathematically true
/// but misleading — the worker did some non-zero work).
fn format_cost_usd(cost: f64) -> String {
    if cost <= 0.0 {
        "$0.00".to_string()
    } else if cost < 0.01 {
        "< $0.01".to_string()
    } else {
        format!("${cost:.2}")
    }
}

fn trunc(s: &str, max_chars: usize) -> &str {
    match s.char_indices().nth(max_chars) {
        Some((byte_idx, _)) => &s[..byte_idx],
        None => s,
    }
}

/// Build a key-value `ListItem` for the detail panel.
fn kv_item(key: &str, val: &str, val_color: Option<Color>) -> ListItem {
    let key_color = Color::rgb(130, 170, 210);
    let text = if key.is_empty() {
        // Blank line or plain value row (e.g. title text)
        StyledText {
            spans: vec![StyledSpan::with_fg(val, Color::rgb(210, 210, 210))],
        }
    } else {
        StyledText {
            spans: vec![
                StyledSpan::with_fg(format!(" {:12} ", key), key_color),
                StyledSpan::with_fg(val, val_color.unwrap_or(Color::rgb(210, 210, 210))),
            ],
        }
    };
    ListItem {
        text,
        icon: None,
        detail: None,
        decoration: Decoration::Normal,
    }
}

// ─── Activity log parsing ─────────────────────────────────────────────────────

/// Build a plain `ListItem` for the Activity feed.
fn activity_item(text: &str, color: Color) -> ListItem {
    ListItem {
        text: StyledText {
            spans: vec![StyledSpan::with_fg(text, color)],
        },
        icon: None,
        detail: None,
        decoration: Decoration::Normal,
    }
}

/// Minimal JSON string-field extractor.
///
/// Finds the first occurrence of `"field":"value"` in `json` and returns
/// the unescaped string value. Returns `None` if the field is absent or
/// its value is not a quoted string. Only handles compact (no-space) JSON,
/// which is what `claude -p --output-format stream-json` emits.
fn json_str(json: &str, field: &str) -> Option<String> {
    let key = format!("\"{}\":\"", field);
    let start = json.find(&key)? + key.len();
    let rest = &json[start..];
    let mut result = String::new();
    let mut chars = rest.chars();
    loop {
        match chars.next()? {
            '"' => break,
            '\\' => {
                match chars.next()? {
                    'n' => result.push(' '),
                    't' => result.push(' '),
                    'r' => {}
                    c => result.push(c),
                }
            }
            c => result.push(c),
        }
    }
    Some(result)
}

/// Minimal JSON numeric-field extractor (handles integers and floats).
fn json_num(json: &str, field: &str) -> Option<f64> {
    // Match `"field":NUMBER` — value is terminated by , } or whitespace.
    let key = format!("\"{}\":", field);
    let pos = json.find(&key)? + key.len();
    let rest = json[pos..].trim_start();
    // Skip null / boolean / quoted values.
    if rest.starts_with('"') || rest.starts_with("null") || rest.starts_with("true") {
        return None;
    }
    let end = rest
        .find(|c: char| c == ',' || c == '}' || c == ']' || c.is_whitespace())
        .unwrap_or(rest.len());
    rest[..end].parse::<f64>().ok()
}

/// Return all tool names found in `"type":"tool_use"` blocks within `json`.
///
/// Searches for each `"type":"tool_use"` marker and extracts the `"name"`
/// field from the same JSON object. Works for both top-level `tool_use`
/// events and tool-use blocks nested inside `assistant` message content.
fn extract_tool_names(json: &str) -> Vec<String> {
    let marker = "\"type\":\"tool_use\"";
    let mut names: Vec<String> = Vec::new();
    let mut pos = 0;
    while let Some(found) = json[pos..].find(marker) {
        let after = pos + found + marker.len();
        // "name" should appear within the next ~200 bytes of the same object.
        // Round window_end up to the next UTF-8 char boundary so we don't slice
        // through a multi-byte glyph like `─` (3 bytes) and panic. Crash repro
        // was a worker for #218 whose Edit tool input contained box-drawing
        // characters that landed exactly on the 200-byte boundary.
        let mut window_end = (after + 200).min(json.len());
        while window_end < json.len() && !json.is_char_boundary(window_end) {
            window_end += 1;
        }
        let window = &json[after..window_end];
        if let Some(name) = json_str(window, "name") {
            if !name.is_empty() && !names.contains(&name) {
                names.push(name);
            }
        }
        pos = after;
    }
    names
}

/// Parse the `REVIEW_VERDICT` / `REVIEW_BODY` block embedded in a result event's
/// `result` string and return it as renderable list items. Returns an empty
/// Vec when the result isn't a structured review (e.g. a normal work or plan
/// completion).
///
/// The expected format is the one declared in the REVIEWER_SYSTEM_PROMPT:
///
/// ```text
/// REVIEW_VERDICT: approve | request-changes
/// REVIEW_BODY:
/// <markdown body lines>
/// END_REVIEW
/// ```
///
/// The body comes back with `\n` escape sequences (JSON-encoded). We
/// un-escape `\\n` → `\n` and `\\"` → `"` before splitting into lines so the
/// rendered output matches the verbatim review.
fn extract_review_items(line: &str) -> Vec<ListItem> {
    let mut items: Vec<ListItem> = Vec::new();
    // Find the verdict marker. The result text is itself JSON-encoded inside
    // the result field, so `\n` appears as the two-char sequence `\\n`.
    let verdict_marker = "REVIEW_VERDICT:";
    let body_marker = "REVIEW_BODY:";
    let end_marker = "END_REVIEW";

    let v_pos = line.find(verdict_marker);
    let b_pos = line.find(body_marker);
    let (Some(v_pos), Some(b_pos)) = (v_pos, b_pos) else { return items; };

    // Verdict word: between the verdict marker and the next newline marker.
    let verdict_after = &line[v_pos + verdict_marker.len()..];
    let verdict_end = verdict_after
        .find("\\n")
        .or_else(|| verdict_after.find('\n'))
        .unwrap_or(verdict_after.len());
    let verdict = verdict_after[..verdict_end].trim().trim_matches(',');

    // Header line, coloured by outcome.
    let color = if verdict == "approve" {
        Color::rgb(100, 220, 130)
    } else if verdict.contains("request-changes") || verdict.contains("fail") {
        Color::rgb(230, 130, 80)
    } else {
        Color::rgb(180, 180, 100)
    };
    items.push(activity_item(&format!("[review] {}", verdict), color));

    // Body: between the body marker and END_REVIEW (if present).
    let body_after = &line[b_pos + body_marker.len()..];
    let body_end_rel = body_after.find(end_marker).unwrap_or(body_after.len());
    let raw_body = &body_after[..body_end_rel];

    // Un-escape the JSON-encoded body so newlines / quotes render normally.
    let mut unescaped = String::with_capacity(raw_body.len());
    let mut chars = raw_body.chars();
    while let Some(c) = chars.next() {
        if c == '\\' {
            match chars.next() {
                Some('n') => unescaped.push('\n'),
                Some('t') => unescaped.push('\t'),
                Some('"') => unescaped.push('"'),
                Some('\\') => unescaped.push('\\'),
                Some(other) => {
                    unescaped.push('\\');
                    unescaped.push(other);
                }
                None => unescaped.push('\\'),
            }
        } else {
            unescaped.push(c);
        }
    }
    // Skip the leading newline(s) right after `REVIEW_BODY:` so the first
    // rendered line is meaningful content rather than a blank row.
    let body = unescaped.trim_start_matches('\n');
    for body_line in body.lines() {
        items.push(activity_item(
            &format!("  {}", body_line),
            Color::rgb(200, 200, 210),
        ));
    }
    items
}

/// Extract the first non-empty text block from an assistant message.
///
/// Looks for `"type":"text"` content blocks and returns the `"text"` field.
/// Returns an empty string if no text block is found.
fn extract_text_block(json: &str) -> String {
    let marker = "\"type\":\"text\"";
    if let Some(pos) = json.find(marker) {
        let after = &json[pos + marker.len()..];
        if let Some(text) = json_str(after, "text") {
            return text;
        }
    }
    String::new()
}

/// Parse one stream-json event line into a displayable `ListItem`.
///
/// Returns `None` for event types that are too noisy to surface (e.g.
/// `tool_result`, `system/task_*`, `rate_limit_event`).
/// `turn_n` is a mutable counter incremented for each `assistant` event.
fn parse_json_event(line: &str, turn_n: &mut usize) -> Option<ListItem> {
    let type_val = json_str(line, "type")?;
    match type_val.as_str() {
        "system" => {
            let subtype = json_str(line, "subtype").unwrap_or_default();
            if subtype == "init" {
                let model = json_str(line, "model").unwrap_or_else(|| "?".to_string());
                return Some(activity_item(
                    &format!("[init] {}", model),
                    Color::rgb(100, 100, 180),
                ));
            }
            // Skip task_started / task_completed / other system subtypes.
            None
        }

        "assistant" => {
            *turn_n += 1;
            let n = *turn_n;

            // Check for STATUS: / STUCK: inside the text block first.
            let text = extract_text_block(line);
            if let Some(idx) = text.find("STATUS:") {
                let rest = &text[idx..];
                let end = rest.find('\n').unwrap_or(rest.len());
                let trimmed = rest[..end].trim();
                return Some(activity_item(trimmed, Color::rgb(80, 210, 80)));
            }
            if let Some(idx) = text.find("STUCK:") {
                let rest = &text[idx..];
                let end = rest.find('\n').unwrap_or(rest.len());
                let trimmed = rest[..end].trim();
                return Some(activity_item(trimmed, Color::rgb(220, 120, 50)));
            }

            // Summarise tool calls in this turn.
            let tools = extract_tool_names(line);
            let summary = if !tools.is_empty() {
                format!("[assistant] Turn {}: tool_use={}", n, tools.join(","))
            } else if !text.is_empty() {
                let display = trunc(&text, 80);
                format!("[assistant] Turn {}: {:?}", n, display)
            } else {
                format!("[assistant] Turn {}", n)
            };
            Some(activity_item(&summary, Color::rgb(150, 180, 240)))
        }

        "tool_use" => {
            let name = json_str(line, "name").unwrap_or_else(|| "?".to_string());
            let detail = match name.as_str() {
                "Bash" => json_str(line, "command")
                    .map(|c| trunc(&c, 60).to_string())
                    .unwrap_or_default(),
                "Edit" | "Write" | "Read" | "Glob" | "NotebookEdit" => {
                    json_str(line, "file_path").unwrap_or_default()
                }
                _ => String::new(),
            };
            let text = if detail.is_empty() {
                format!("[tool] {}", name)
            } else {
                format!("[tool] {}: {}", name, detail)
            };
            Some(activity_item(&text, Color::rgb(180, 150, 220)))
        }

        "result" => {
            let turns = json_num(line, "num_turns").unwrap_or(0.0) as u64;
            let cost = json_num(line, "total_cost_usd").unwrap_or(0.0);
            let stop = json_str(line, "stop_reason").unwrap_or_else(|| "?".to_string());
            let dur_ms = json_num(line, "duration_ms").unwrap_or(0.0) as u64;
            let dur = fmt_dur(dur_ms / 1000);
            let text = format!(
                "[result] {} turns  ${:.2}  {}  stop={}",
                turns, cost, dur, stop
            );
            // Review-verdict body extraction happens in parse_log_content so
            // it can emit multiple list items below the metrics line.
            Some(activity_item(&text, Color::rgb(200, 200, 100)))
        }

        "rate_limit_event" => Some(activity_item(
            "[rate_limit]",
            Color::rgb(220, 150, 50),
        )),

        _ => None,
    }
}

/// Parse log content (NDJSON stream-json or plain text) into displayable `ListItem`s.
///
/// Handles both stream-json logs (NDJSON from `claude -p --output-format
/// stream-json`) and plain-text logs (stdout of older workers).
fn parse_log_content(content: &str) -> Vec<ListItem> {
    // Detect format: stream-json if the first non-comment non-blank line
    // starts with `{`.
    let is_json = content
        .lines()
        .find(|l| !l.starts_with('#') && !l.trim().is_empty())
        .map(|l| l.trim_start().starts_with('{'))
        .unwrap_or(false);

    let mut items: Vec<ListItem> = Vec::new();
    let mut turn_n: usize = 0;

    for line in content.lines() {
        if line.starts_with('#') || line.trim().is_empty() {
            continue;
        }

        if is_json {
            if let Some(item) = parse_json_event(line, &mut turn_n) {
                items.push(item);
            }
            // After the metrics line, surface the structured review verdict
            // (REVIEW_VERDICT / REVIEW_BODY) embedded in the `result` field
            // of result events so reviewers don't have to leave the TUI to
            // see what the reviewer said.
            if line.contains("\"type\":\"result\"") {
                items.extend(extract_review_items(line));
            }
        } else {
            // Plain-text log: surface STATUS: / STUCK: lines.
            if line.contains("STATUS:") {
                if let Some(idx) = line.find("STATUS:") {
                    let rest = line[idx..].trim();
                    items.push(activity_item(rest, Color::rgb(80, 210, 80)));
                }
            } else if line.contains("STUCK:") {
                if let Some(idx) = line.find("STUCK:") {
                    let rest = line[idx..].trim();
                    items.push(activity_item(rest, Color::rgb(220, 120, 50)));
                }
            }
        }
    }

    if items.is_empty() {
        items.push(kv_item(
            "",
            "  No activity yet",
            Some(Color::rgb(100, 100, 100)),
        ));
    }

    items
}

/// Read `~/.coord/logs/<id>.log` and return displayable `ListItem`s.
///
/// If the log file doesn't exist locally, returns a single "remote assignment"
/// notice. Callers that know the machine name should use
/// [`CoordApp::get_activity_log`] instead, which fetches from the remote agent.
fn load_activity_log(id: &str) -> Vec<ListItem> {
    let path = coord_dir().join("logs").join(format!("{}.log", id));

    let content = match std::fs::read_to_string(&path) {
        Ok(c) => c,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return vec![kv_item(
                "",
                "  Log not available (remote assignment)",
                Some(Color::rgb(100, 100, 100)),
            )];
        }
        Err(e) => {
            return vec![kv_item(
                "",
                &format!("  Error reading log: {}", e),
                Some(Color::rgb(220, 70, 70)),
            )];
        }
    };

    parse_log_content(&content)
}

// ─── Pipeline stage rendering ─────────────────────────────────────────────────

/// Whether a pipeline stage can be dispatched by clicking `[Go]` in the
/// Pipeline panel.  Stages outside this list still render — they just
/// don't show a button (they're driven implicitly by the coordinator).
fn is_dispatchable_stage(name: &str) -> bool {
    matches!(name, "plan" | "work" | "review" | "merge")
}

/// Build a `ListItem` for one pipeline stage (plan/work/review/smoke).
///
/// `assignment` is the best-matching assignment for the stage, or `None`
/// when the stage hasn't started yet.
fn pipeline_stage_item(name: &str, assignment: Option<&Assignment>) -> ListItem {
    let (indicator, color, detail_str) = match assignment {
        None => (
            "  -",
            Color::rgb(100, 100, 100),
            "pending".to_string(),
        ),
        Some(a) => match a.status.as_str() {
            "running" => (
                "  ~",
                Color::rgb(80, 220, 80),
                format!("{}  {}", trunc(&a.id, 8), a.age_str()),
            ),
            "done" => (
                "  ✓",
                Color::rgb(120, 200, 120),
                format!("{}  {}", trunc(&a.id, 8), a.age_str()),
            ),
            "failed" => (
                "  ✗",
                Color::rgb(220, 70, 70),
                format!("{}  {}", trunc(&a.id, 8), a.age_str()),
            ),
            _ => (
                "  ?",
                Color::rgb(200, 200, 70),
                format!("{}  {}", trunc(&a.id, 8), a.age_str()),
            ),
        },
    };
    ListItem {
        text: StyledText {
            spans: vec![
                StyledSpan::with_fg(indicator, color),
                StyledSpan::with_fg(
                    format!(" {:8}", name),
                    Color::rgb(180, 180, 200),
                ),
                StyledSpan::with_fg(format!("  {}", detail_str), color),
            ],
        },
        icon: None,
        detail: None,
        decoration: if assignment.map(|a| a.status.as_str()) == Some("failed") {
            Decoration::Error
        } else {
            Decoration::Normal
        },
    }
}

/// Build a `ListItem` for the PR/Merge stage, sourced from `merge_queue`.
fn pipeline_merge_item(entry: Option<&MergeQueueEntry>) -> ListItem {
    let (indicator, color, detail_str) = match entry {
        None => (
            "  -",
            Color::rgb(100, 100, 100),
            "pending".to_string(),
        ),
        Some(e) => {
            let pr_label = match e.pr_number {
                Some(n) => format!("PR #{}", n),
                None => e.state.clone(),
            };
            match e.state.as_str() {
                "merged" => ("  ✓", Color::rgb(120, 200, 120), pr_label),
                "open" | "queued" => ("  ~", Color::rgb(80, 220, 80), pr_label),
                "failed" => ("  ✗", Color::rgb(220, 70, 70), pr_label),
                "human_required" => ("  !", Color::rgb(220, 100, 100), "needs manual rebase".to_string()),
                _ => ("  -", Color::rgb(100, 100, 100), pr_label),
            }
        }
    };
    ListItem {
        text: StyledText {
            spans: vec![
                StyledSpan::with_fg(indicator, color),
                StyledSpan::with_fg(" PR/Merge".to_string(), Color::rgb(180, 180, 200)),
                StyledSpan::with_fg(format!("  {}", detail_str), color),
            ],
        },
        icon: None,
        detail: None,
        decoration: match entry.map(|e| e.state.as_str()) {
            Some("failed") | Some("human_required") => Decoration::Error,
            _ => Decoration::Normal,
        },
    }
}

// ─── Data loading ─────────────────────────────────────────────────────────────

fn home_dir() -> PathBuf {
    std::env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/root"))
}

fn coord_dir() -> PathBuf {
    home_dir().join(".coord")
}

/// TCP probe on port 7433 with a 150 ms deadline.
/// Hostname resolution is included in the deadline via a background thread.
fn tcp_probe(host: &str, port: u16) -> bool {
    use std::sync::mpsc;
    let host = host.to_string();
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let addr_str = format!("{}:{}", host, port);
        let ok = addr_str
            .to_socket_addrs()
            .ok()
            .and_then(|mut it| it.next())
            .map(|addr| TcpStream::connect_timeout(&addr, Duration::from_millis(120)).is_ok())
            .unwrap_or(false);
        let _ = tx.send(ok);
    });
    rx.recv_timeout(Duration::from_millis(200)).unwrap_or(false)
}

fn load_data() -> BoardData {
    let dir = coord_dir();
    let db_path = dir.join("coord.db");

    // Open the DB read-only; return empty data if the DB doesn't exist yet.
    let conn = match Connection::open_with_flags(
        &db_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    ) {
        Ok(c) => c,
        Err(_) => return BoardData::default(),
    };

    // ── Query assignments ──────────────────────────────────────────────────
    // dispatched_at and finished_at are stored as REAL (Unix float seconds).
    let mut assignments: Vec<Assignment> = {
        let mut stmt = match conn.prepare(
            "SELECT assignment_id, machine_name, repo_name, issue_number, issue_title, \
             status, branch, model, type, dispatched_at, finished_at, exit_code, \
             test_state, review_verdict, review_of_assignment_id, cost_usd, \
             smoke_tests, review_findings \
             FROM assignments ORDER BY dispatched_at DESC",
        ) {
            Ok(s) => s,
            Err(_) => return BoardData::default(),
        };
        let rows = match stmt.query_map([], |row| {
            Ok(Assignment {
                id: row.get::<_, String>(0)?,
                machine: row.get::<_, String>(1)?,
                repo: row.get::<_, String>(2)?,
                issue_number: row.get::<_, i64>(3)? as u64,
                issue_title: row.get::<_, String>(4)?,
                status: row.get::<_, String>(5)?,
                branch: row.get::<_, Option<String>>(6)?,
                model: row.get::<_, Option<String>>(7)?,
                assignment_type: row.get::<_, Option<String>>(8)?,
                dispatched_at: row.get::<_, Option<f64>>(9)?,
                finished_at: row.get::<_, Option<f64>>(10)?,
                exit_code: row.get::<_, Option<i32>>(11)?,
                test_state: row.get::<_, Option<String>>(12)?,
                review_verdict: row.get::<_, Option<String>>(13)?,
                review_of_assignment_id: row.get::<_, Option<String>>(14)?,
                cost_usd: row.get::<_, Option<f64>>(15)?,
                smoke_tests: row
                    .get::<_, Option<String>>(16)?
                    .and_then(|raw| serde_json::from_str::<Vec<String>>(&raw).ok()),
                review_findings: row.get::<_, Option<String>>(17)?,
            })
        }) {
            Ok(r) => r,
            Err(_) => return BoardData::default(),
        };
        rows.filter_map(|r| r.ok()).collect()
    };

    // Sort: running first, then failed, then done (most recent first within groups).
    assignments.sort_by(|a, b| {
        let rank = |s: &str| match s {
            "running" => 0u8,
            "failed" => 1,
            "done" => 2,
            _ => 3,
        };
        rank(&a.status).cmp(&rank(&b.status)).then_with(|| {
            b.dispatched_at
                .partial_cmp(&a.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        })
    });

    // ── Query machines (name = nickname, host = Tailscale FQDN, repos = JSON array) ─
    let machine_rows: Vec<(String, String, Vec<String>)> = {
        let mut stmt = match conn.prepare("SELECT name, host, repos FROM machines") {
            Ok(s) => s,
            Err(_) => {
                return BoardData {
                    assignments,
                    ..BoardData::default()
                }
            }
        };
        let rows = match stmt.query_map([], |row| {
            let repos_json: String = row.get::<_, Option<String>>(2)?.unwrap_or_else(|| "[]".to_string());
            let repos: Vec<String> = serde_json::from_str(&repos_json).unwrap_or_default();
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?, repos))
        }) {
            Ok(r) => r,
            Err(_) => {
                return BoardData {
                    assignments,
                    ..BoardData::default()
                }
            }
        };
        rows.filter_map(|r| r.ok()).collect()
    };

    // ── Machine reachability probes ────────────────────────────────────────
    // Probe using the Tailscale host (fixes #121: machine name ≠ Tailscale hostname).
    // Spawn all probes concurrently; collect within a 250 ms budget.
    let probes: Vec<(String, String, Vec<String>, std::sync::mpsc::Receiver<bool>)> = machine_rows
        .iter()
        .map(|(name, host, repos)| {
            use std::sync::mpsc;
            let h = host.clone();
            let (tx, rx) = mpsc::channel();
            std::thread::spawn(move || {
                let _ = tx.send(tcp_probe(&h, 7433));
            });
            (name.clone(), host.clone(), repos.clone(), rx)
        })
        .collect();

    let machines: Vec<Machine> = probes
        .into_iter()
        .map(|(name, host, repos, rx)| {
            let reachable = rx.recv_timeout(Duration::from_millis(250)).unwrap_or(false);
            let active_count = assignments
                .iter()
                .filter(|a| a.machine == name && a.status == "running")
                .count();
            Machine {
                name,
                host,
                reachable,
                active_count,
                repos,
            }
        })
        .collect();

    // ── Determine which machine is local ──────────────────────────────────
    // Match the OS hostname against the `host` column in the machines table.
    let local_hostname = gethostname::gethostname()
        .into_string()
        .unwrap_or_default();
    let local_machine = machine_rows
        .iter()
        .find(|(_, host, _)| *host == local_hostname)
        .map(|(name, _, _)| name.clone())
        .unwrap_or_default();

    // ── Query merge_queue ──────────────────────────────────────────────────
    // Join to assignments to resolve issue_number (merge_queue may not have it).
    let merge_queue: Vec<MergeQueueEntry> = {
        let mut stmt = match conn.prepare(
            "SELECT mq.assignment_id, a.issue_number, mq.state, mq.pr_number, mq.pr_url, mq.repo_github \
             FROM merge_queue mq \
             LEFT JOIN assignments a ON mq.assignment_id = a.assignment_id",
        ) {
            Ok(s) => s,
            Err(_) => {
                // merge_queue table may not exist yet — return what we have.
                return BoardData {
                    local_machine,
                    assignments,
                    machines,
                    ..BoardData::default()
                };
            }
        };
        let rows = match stmt.query_map([], |row| {
            Ok(MergeQueueEntry {
                assignment_id: row.get::<_, String>(0)?,
                issue_number: row
                    .get::<_, Option<i64>>(1)?
                    .map(|n| n as u64),
                state: row.get::<_, String>(2)?,
                pr_number: row.get::<_, Option<i64>>(3)?,
                pr_url: row.get::<_, Option<String>>(4)?,
                repo_github: row.get::<_, String>(5)?,
            })
        }) {
            Ok(r) => r,
            Err(_) => {
                return BoardData {
                    local_machine,
                    assignments,
                    machines,
                    ..BoardData::default()
                };
            }
        };
        rows.filter_map(|r| r.ok()).collect()
    };

    // ── Query proposals ───────────────────────────────────────────────────
    let proposals: Vec<Proposal> = {
        let mut stmt = match conn.prepare(
            "SELECT id, machine_name, repo_name, issue_number, issue_title, \
             rationale, type FROM proposals ORDER BY id",
        ) {
            Ok(s) => s,
            Err(_) => {
                return BoardData {
                    local_machine,
                    assignments,
                    machines,
                    merge_queue,
                    ..BoardData::default()
                };
            }
        };
        let rows = match stmt.query_map([], |row| {
            Ok(Proposal {
                id: row.get::<_, i64>(0)?,
                machine: row.get::<_, String>(1)?,
                repo: row.get::<_, String>(2)?,
                issue_number: row.get::<_, i64>(3)? as u64,
                issue_title: row.get::<_, String>(4)?,
                rationale: row.get::<_, Option<String>>(5)?.unwrap_or_default(),
                proposal_type: row.get::<_, Option<String>>(6)?.unwrap_or_else(|| "work".into()),
            })
        }) {
            Ok(r) => r,
            Err(_) => {
                return BoardData {
                    local_machine,
                    assignments,
                    machines,
                    merge_queue,
                    ..BoardData::default()
                };
            }
        };
        rows.filter_map(|r| r.ok()).collect()
    };

    // ── Query synced issues (both open and closed) ─────────────────────────
    // Loaded eagerly so the Board Issue tab can show bodies for issues in
    // any lifecycle group, including closed ones in Completed. Only the
    // "open" entries are injected as Pending rows downstream.
    let open_issues: Vec<OpenIssue> = {
        let mut stmt = match conn.prepare(
            "SELECT repo_name, number, title, body, labels, state FROM issues \
             ORDER BY repo_name, number",
        ) {
            Ok(s) => s,
            Err(_) => return BoardData { local_machine, assignments, machines, merge_queue, proposals, ..BoardData::default() },
        };
        let rows = match stmt.query_map([], |row| {
            let labels_raw: String = row.get(4).unwrap_or_default();
            let labels: Vec<String> = serde_json::from_str(&labels_raw).unwrap_or_default();
            Ok(OpenIssue {
                repo_name: row.get::<_, String>(0)?,
                number: row.get::<_, i64>(1)? as u64,
                title: row.get::<_, String>(2)?,
                body: row.get::<_, String>(3).unwrap_or_default(),
                labels,
                state: row.get::<_, String>(5).unwrap_or_else(|_| "open".to_string()),
            })
        }) {
            Ok(r) => r,
            Err(_) => return BoardData { local_machine, assignments, machines, merge_queue, proposals, ..BoardData::default() },
        };
        rows.filter_map(|r| r.ok()).collect()
    };

    // ── Query board_meta for pipeline config ───────────────────────────────
    let (
        pipeline_default_gates,
        pipeline_tracked_labels,
        pipeline_repos,
        pipeline_require_plan,
    ) = load_pipeline_meta(&conn);

    BoardData {
        local_machine,
        assignments,
        open_issues,
        machines,
        merge_queue,
        proposals,
        pipeline_default_gates,
        pipeline_tracked_labels,
        pipeline_repos,
        pipeline_require_plan,
    }
}

/// Spawn a background thread that calls [`load_data`] and sends the result
/// over a channel.  The caller polls the returned [`Receiver`] without
/// blocking the UI thread.
fn start_data_load() -> std::sync::mpsc::Receiver<BoardData> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(load_data());
    });
    rx
}

/// Spawn a background thread that fetches a remote agent log over HTTP.
///
/// Returns a `Receiver` that yields `Ok(raw_content)` or `Err(error_message)`.
/// The caller must parse the content with [`parse_log_content`] on the main
/// thread — keeping `ListItem` construction off the worker thread.
fn spawn_log_fetch(host: &str, id: &str) -> std::sync::mpsc::Receiver<Result<String, String>> {
    let (tx, rx) = std::sync::mpsc::channel();
    let url = format!("http://{}:7433/logs/{}", host, id);
    std::thread::spawn(move || {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(std::time::Duration::from_secs(5))
            .timeout(std::time::Duration::from_secs(5))
            .build();
        let result = match agent.get(&url).call() {
            Ok(resp) => resp.into_string().map_err(|e| e.to_string()),
            Err(e) => Err(e.to_string()),
        };
        let _ = tx.send(result);
    });
    rx
}

/// Spawn a `gh issue view` for a single issue and parse the response into a
/// [`FetchedIssue`]. Used by the Board Issue tab when the issue isn't in the
/// local `issues` table (e.g. closed >7d ago and pruned).
///
/// On success, also upserts the row into the local `issues` table so the
/// fetch becomes durable — the next `load_data` finds it and we don't repeat
/// the gh call on the next session. The upsert uses a writer connection with
/// busy_timeout=5s, the same pattern as the purge/test-verdict writers.
fn spawn_issue_fetch(
    repo_slug: String,
    repo_name: String,
    number: u64,
) -> std::sync::mpsc::Receiver<Result<FetchedIssue, String>> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let output = std::process::Command::new("gh")
            .args([
                "issue",
                "view",
                &number.to_string(),
                "--repo",
                &repo_slug,
                "--json",
                "number,title,body,labels,state",
            ])
            .output();
        let result = match output {
            Ok(o) if o.status.success() => {
                match serde_json::from_slice::<serde_json::Value>(&o.stdout) {
                    Ok(v) => {
                        let labels: Vec<String> = v
                            .get("labels")
                            .and_then(|l| l.as_array())
                            .map(|arr| {
                                arr.iter()
                                    .filter_map(|l| {
                                        l.get("name").and_then(|n| n.as_str()).map(String::from)
                                    })
                                    .collect()
                            })
                            .unwrap_or_default();
                        let issue = FetchedIssue {
                            number,
                            title: v.get("title").and_then(|s| s.as_str()).unwrap_or("").to_string(),
                            body: v.get("body").and_then(|s| s.as_str()).unwrap_or("").to_string(),
                            labels,
                            state: v
                                .get("state")
                                .and_then(|s| s.as_str())
                                .unwrap_or("open")
                                .to_ascii_lowercase(),
                        };
                        // Best-effort upsert into the local DB. Failures (DB
                        // locked, schema missing, etc.) are non-fatal — the
                        // in-memory cache still serves the body for the rest
                        // of the session.
                        let _ = upsert_issue_db(&repo_name, &issue);
                        Ok(issue)
                    }
                    Err(e) => Err(format!("gh json parse failed: {}", e)),
                }
            }
            Ok(o) => Err(String::from_utf8_lossy(&o.stderr).trim().to_string()),
            Err(e) => Err(format!("could not run gh: {}", e)),
        };
        let _ = tx.send(result);
    });
    rx
}

/// #271 part 2 follow-up: spawn a background `gh pr view` to fetch the
/// PR title, body, and files-changed list for a single PR.  Mirrors
/// `spawn_issue_fetch`: same channel-receiver shape, same lifecycle in
/// the caching maps on `CoordApp` (`pending_pr_fetches` →
/// `fetched_prs_cache`).
fn spawn_pr_fetch(
    repo_slug: String,
    pr_number: i64,
) -> std::sync::mpsc::Receiver<Result<FetchedPr, String>> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let output = std::process::Command::new("gh")
            .args([
                "pr", "view",
                &pr_number.to_string(),
                "--repo", &repo_slug,
                "--json", "title,body,files,reviews",
            ])
            .output();
        let result = match output {
            Ok(o) if o.status.success() => {
                match serde_json::from_slice::<serde_json::Value>(&o.stdout) {
                    Ok(v) => {
                        let files: Vec<String> = v
                            .get("files")
                            .and_then(|f| f.as_array())
                            .map(|arr| {
                                arr.iter()
                                    .filter_map(|f| {
                                        f.get("path").and_then(|n| n.as_str()).map(String::from)
                                    })
                                    .collect()
                            })
                            .unwrap_or_default();
                        let reviews: Vec<FetchedReview> = v
                            .get("reviews")
                            .and_then(|r| r.as_array())
                            .map(|arr| {
                                arr.iter()
                                    .map(|r| FetchedReview {
                                        state: r
                                            .get("state")
                                            .and_then(|s| s.as_str())
                                            .unwrap_or("")
                                            .to_string(),
                                        body: r
                                            .get("body")
                                            .and_then(|s| s.as_str())
                                            .unwrap_or("")
                                            .to_string(),
                                    })
                                    .collect()
                            })
                            .unwrap_or_default();
                        Ok(FetchedPr {
                            title: v.get("title").and_then(|s| s.as_str()).unwrap_or("").to_string(),
                            body: v.get("body").and_then(|s| s.as_str()).unwrap_or("").to_string(),
                            files,
                            reviews,
                        })
                    }
                    Err(e) => Err(format!("gh json parse failed: {}", e)),
                }
            }
            Ok(o) => Err(String::from_utf8_lossy(&o.stderr).trim().to_string()),
            Err(e) => Err(format!("could not run gh: {}", e)),
        };
        let _ = tx.send(result);
    });
    rx
}

/// Upsert a freshly-fetched issue into the local `issues` table. Mirrors the
/// `upsert_open_issues` Python helper but for a single row, using the same
/// connection-with-busy-timeout pattern as the other TUI writers (purge,
/// test-verdict). Single-statement transaction, safe under concurrent
/// coord/TUI writers per SQLite WAL semantics.
fn upsert_issue_db(repo_name: &str, issue: &FetchedIssue) -> rusqlite::Result<()> {
    let conn = open_purge_conn()?;
    let labels_json =
        serde_json::to_string(&issue.labels).unwrap_or_else(|_| "[]".to_string());
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    conn.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7) \
         ON CONFLICT(repo_name, number) DO UPDATE SET \
            title = excluded.title, \
            body = excluded.body, \
            state = excluded.state, \
            labels = excluded.labels, \
            synced_at = excluded.synced_at",
        rusqlite::params![
            repo_name,
            issue.number as i64,
            issue.title,
            issue.body,
            issue.state,
            labels_json,
            now
        ],
    )?;
    Ok(())
}

/// Spawn a background thread that opens a Server-Sent Events connection to
/// `http://{host}:7433/stream/{id}`, parses SSE events, and sends them over
/// the returned `Receiver`.
///
/// ## Resume support
/// Pass `last_event_id > 0` to resume from a previous byte-offset by sending
/// the `Last-Event-Id` header.  The agent's `/stream/{id}` endpoint uses the
/// byte offset as the event id, so the stream resumes from that position.
///
/// ## Cancellation
/// Drop the returned `Receiver` to signal the thread to exit.  The thread
/// detects this on the next `tx.send()` call (which returns `Err`).  Under
/// normal conditions this happens within 15 s (SSE keepalive interval); a
/// 20-second read timeout acts as a safety net if keepalives stop.
fn spawn_sse_watch(host: &str, id: &str, last_event_id: u64) -> std::sync::mpsc::Receiver<SseWatchMsg> {
    let (tx, rx) = std::sync::mpsc::channel();
    let url = format!("http://{}:7433/stream/{}", host, id);
    std::thread::spawn(move || {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(std::time::Duration::from_secs(5))
            // 20 s read timeout. The server sends SSE keepalives every 15 s so
            // this fires only when the connection is genuinely dead.
            .timeout_read(std::time::Duration::from_secs(20))
            .build();

        let mut builder = agent.get(&url);
        if last_event_id > 0 {
            builder = builder.set("Last-Event-Id", &last_event_id.to_string());
        }

        let resp = match builder.call() {
            Ok(r) => r,
            Err(e) => {
                let _ = tx.send(SseWatchMsg::Error(e.to_string()));
                return;
            }
        };

        use std::io::BufRead;
        let reader = std::io::BufReader::new(resp.into_reader());

        let mut current_id = last_event_id;
        let mut current_event = String::new();
        let mut current_data: Vec<String> = Vec::new();

        for line_result in reader.lines() {
            let line = match line_result {
                Ok(l) => l,
                Err(e) => {
                    // Read error (timeout, connection reset, etc.).
                    let _ = tx.send(SseWatchMsg::Error(e.to_string()));
                    return;
                }
            };

            // Empty line = dispatch the current accumulated event.
            if line.is_empty() {
                if !current_event.is_empty() || !current_data.is_empty() {
                    let text = current_data.join("\n");
                    let keep_going = match current_event.as_str() {
                        "log" => tx
                            .send(SseWatchMsg::Lines { last_id: current_id, text })
                            .is_ok(),
                        "end" => {
                            let _ = tx.send(SseWatchMsg::Done { last_id: current_id });
                            return;
                        }
                        _ => true, // unknown event type — ignore
                    };
                    if !keep_going {
                        return; // receiver was dropped; exit cleanly
                    }
                    current_event.clear();
                    current_data.clear();
                }
                continue;
            }

            // SSE comment / keepalive — send a Heartbeat so the thread
            // discovers a dropped receiver (cancel) within one keepalive period.
            if line.starts_with(':') {
                if tx.send(SseWatchMsg::Heartbeat).is_err() {
                    return;
                }
                continue;
            }

            // SSE field lines.
            if let Some(v) = line.strip_prefix("id: ") {
                current_id = v.trim().parse().unwrap_or(current_id);
            } else if let Some(v) = line.strip_prefix("event: ") {
                current_event = v.trim().to_string();
            } else if let Some(v) = line.strip_prefix("data: ") {
                current_data.push(v.to_string());
            }
            // retry: lines are ignored — the main thread owns reconnect logic.
        }

        // EOF: connection closed without an explicit `end` event.
        let _ = tx.send(SseWatchMsg::Done { last_id: current_id });
    });
    rx
}

/// Width of one arrow connector between stages, in TUI cells. Mirrors the
/// constant used by quadraui's `tui_pipeline_view_layout` so host
/// hit-testing matches the painted geometry.
const PIPELINE_ARROW_WIDTH: f32 = 4.0;
/// Height of the action-button row when any stage has an action.
const PIPELINE_ACTION_HEIGHT: f32 = 1.0;

/// Compute the PipelineView layout that the TUI backend would paint into
/// `rect`. Lets `mouse_main_click` hit-test without holding a `Backend`.
///
/// Matches the constants used by `quadraui::tui::tui_pipeline_view_layout`;
/// if those drift, the GTK and TUI flows could disagree on stage bounds.
fn tui_pipeline_layout(
    view: &QuiPipelineView,
    rect: Rect,
) -> quadraui::PipelineViewLayout {
    let action_h = if view.stages.iter().any(|s| s.action.is_some()) {
        PIPELINE_ACTION_HEIGHT
    } else {
        0.0
    };
    view.layout(
        rect.x,
        rect.y,
        quadraui::PipelineViewMeasure::new(
            rect.width,
            rect.height,
            PIPELINE_ARROW_WIDTH,
            action_h,
        ),
    )
}

/// Status badge text + colour for the Pipeline sidebar row.
fn stage_badge(stage: &str) -> (String, Color) {
    match stage {
        "work" => ("work".into(), Color::rgb(150, 200, 240)),
        "review" => ("review".into(), Color::rgb(200, 180, 100)),
        "smoke" => ("smoke".into(), Color::rgb(180, 150, 220)),
        "merge" => ("merge".into(), Color::rgb(100, 180, 240)),
        "done" => ("done".into(), Color::rgb(120, 200, 120)),
        other => (other.to_string(), Color::rgb(180, 180, 180)),
    }
}

/// Fetch open GitHub issues with at least one of `labels`, via `gh search
/// issues`.  Results are mapped back to coord-local repo names using
/// `repos` (coord_name → owner/repo slug).
///
/// Implementation note: the function shells out to `gh`, parses the JSON
/// blob it emits, and constructs a `PipelineLoaderResult`.  It runs in a
/// background thread spawned from `maybe_kick_pipeline_loader` so the UI
/// thread is never blocked on `gh`.
fn fetch_pipeline_issues(
    labels: &[String],
    repos: &[(String, String)],
) -> PipelineLoaderResult {
    if labels.is_empty() {
        return PipelineLoaderResult::Ok(Vec::new());
    }

    // Build inverse lookup: github slug → coord-local repo name.
    let mut slug_to_local: std::collections::HashMap<String, String> =
        std::collections::HashMap::new();
    for (local, slug) in repos {
        slug_to_local.insert(slug.clone(), local.clone());
    }

    // Use --label and --state flags (not a query string — gh search issues
    // ignores label:/state: qualifiers in the positional query argument).
    // Scope to the configured repos to avoid noise from unrelated repos that
    // happen to share the same label name (e.g. gcc-postcommit-ci uses "coord").
    //
    // `gh search issues --state` only accepts "open" or "closed" (not "all"
    // — that's a `gh issue list` flag). To populate the Done lifecycle
    // section we have to query both states and merge.
    let label_set: std::collections::HashSet<&str> =
        labels.iter().map(|s| s.as_str()).collect();
    let mut issues: Vec<PipelineIssue> = Vec::new();
    for state in ["open", "closed"] {
        let mut args: Vec<String> = vec![
            "search".into(),
            "issues".into(),
            "--state".into(),
            state.into(),
            "--json".into(),
            "number,title,body,labels,repository,url,state".into(),
            "--limit".into(),
            "200".into(),
        ];
        for label in labels {
            args.push("--label".into());
            args.push(label.clone());
        }
        for (_local, slug) in repos {
            args.push("--repo".into());
            args.push(slug.clone());
        }

        let output = std::process::Command::new("gh").args(&args).output();

        let stdout = match output {
            Ok(o) if o.status.success() => o.stdout,
            Ok(o) => {
                return PipelineLoaderResult::Err(
                    String::from_utf8_lossy(&o.stderr).trim().to_string(),
                );
            }
            Err(e) => return PipelineLoaderResult::Err(format!("could not run gh: {}", e)),
        };

        let value: serde_json::Value = match serde_json::from_slice(&stdout) {
            Ok(v) => v,
            Err(e) => return PipelineLoaderResult::Err(format!("gh JSON parse: {}", e)),
        };

        let arr = match value.as_array() {
            Some(a) => a.clone(),
            None => continue,
        };

        for item in &arr {
        let number = item
            .get("number")
            .and_then(|n| n.as_u64())
            .unwrap_or(0);
        if number == 0 {
            continue;
        }
        let title = item
            .get("title")
            .and_then(|t| t.as_str())
            .unwrap_or("")
            .to_string();
        let repo_slug = item
            .get("repository")
            .and_then(|r| r.get("nameWithOwner"))
            .and_then(|s| s.as_str())
            .map(|s| s.to_string())
            .or_else(|| {
                // `gh search issues` sometimes returns the repo as a string url-tail.
                item.get("url").and_then(|u| u.as_str()).and_then(|u| {
                    // https://<host>/owner/name/issues/123 — strip scheme+host,
                    // then take the first two path segments as owner/repo.
                    let path = u.splitn(4, "//").nth(1).unwrap_or(u);
                    let mut parts = path.splitn(4, '/');
                    parts.next(); // skip host
                    let owner = parts.next()?;
                    let repo = parts.next()?;
                    if owner.is_empty() || repo.is_empty() {
                        None
                    } else {
                        Some(format!("{}/{}", owner, repo))
                    }
                })
            })
            .unwrap_or_default();
        let issue_labels: Vec<String> = item
            .get("labels")
            .and_then(|l| l.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|x| x.get("name").and_then(|n| n.as_str()).map(|s| s.to_string()))
                    .collect()
            })
            .unwrap_or_default();
        let matched_labels: Vec<String> = issue_labels
            .iter()
            .filter(|l| label_set.contains(l.as_str()))
            .cloned()
            .collect();
        let body = item
            .get("body")
            .and_then(|b| b.as_str())
            .unwrap_or("")
            .to_string();
        let coord_repo = slug_to_local.get(&repo_slug).cloned();
        let is_closed = item
            .get("state")
            .and_then(|s| s.as_str())
            .map(|s| s == "closed")
            .unwrap_or(false);
        let all_labels = issue_labels;
        issues.push(PipelineIssue {
            number,
            title,
            body,
            repo_slug,
            coord_repo,
            matched_labels,
            all_labels,
            is_closed,
        });
        }
    }
    // Stable order: by repo, then by issue number.
    issues.sort_by(|a, b| a.repo_slug.cmp(&b.repo_slug).then(a.number.cmp(&b.number)));
    PipelineLoaderResult::Ok(issues)
}

/// Fetch CI check summary for one PR by shelling out to `gh pr checks`.
///
/// Mirrors what `coord/ci_github.py::GitHubCi.list_checks_for_pr` does on the
/// Python side, but only computes the rolled-up counts the TUI needs. The
/// returned `String` error is surfaced as a one-line status hint; the TUI
/// silently retries on the next refresh.
fn fetch_ci_check_summary(repo_slug: &str, pr_number: i64) -> Result<CiCheckSummary, String> {
    let args = [
        "pr".to_string(), "checks".to_string(), pr_number.to_string(),
        "--repo".to_string(), repo_slug.to_string(),
        "--json".to_string(), "name,state,conclusion,link".to_string(),
    ];
    let output = std::process::Command::new("gh")
        .args(&args)
        .output()
        .map_err(|e| format!("could not run gh: {}", e))?;
    // `gh pr checks` exits non-zero when any check has failed but stdout is
    // still valid JSON — only treat empty stdout as a real lookup failure.
    let stdout = &output.stdout;
    if stdout.is_empty() && !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().to_string());
    }
    let value: serde_json::Value = serde_json::from_slice(stdout)
        .map_err(|e| format!("gh JSON parse: {}", e))?;
    let arr = value.as_array().cloned().unwrap_or_default();

    let mut passed = 0usize;
    let mut failed = 0usize;
    let mut running = 0usize;
    let mut failed_names: Vec<String> = Vec::new();
    let mut first_failed_url: Option<String> = None;
    for item in &arr {
        let state = item.get("state").and_then(|s| s.as_str()).unwrap_or("").to_lowercase();
        let conclusion = item
            .get("conclusion")
            .and_then(|s| s.as_str())
            .unwrap_or("")
            .to_lowercase();
        let name = item.get("name").and_then(|s| s.as_str()).unwrap_or("").to_string();
        let link = item.get("link").and_then(|s| s.as_str()).unwrap_or("").to_string();
        let is_completed = state == "completed" || state == "complete";
        if !is_completed {
            running += 1;
            continue;
        }
        match conclusion.as_str() {
            "success" => passed += 1,
            "failure" | "cancelled" | "timed_out" | "action_required" => {
                failed += 1;
                failed_names.push(name);
                if first_failed_url.is_none() && !link.is_empty() {
                    first_failed_url = Some(link);
                }
            }
            // skipped / neutral — count as passing for gate purposes
            _ => passed += 1,
        }
    }

    Ok(CiCheckSummary {
        passed,
        failed,
        running,
        failed_names,
        first_failed_url,
        fetched_at: Instant::now(),
    })
}

/// Read pipeline-related entries from the `board_meta` table.
///
/// Returns ``(default_gates, tracked_labels, repos, require_plan)`` with
/// the documented fallbacks when the keys are missing or unparseable:
/// gates default to ``["review", "merge"]``, tracked labels to
/// ``["coord"]``, repos to an empty list, and require_plan to ``false``.
/// Repos are returned as ``(coord_name, github_slug)`` pairs preserving
/// insertion order.
fn load_pipeline_meta(
    conn: &Connection,
) -> (Vec<String>, Vec<String>, Vec<(String, String)>, bool) {
    fn read_key(conn: &Connection, key: &str) -> Option<String> {
        conn.query_row(
            "SELECT value FROM board_meta WHERE key = ?1",
            [key],
            |row| row.get::<_, String>(0),
        )
        .ok()
    }

    let default_gates: Vec<String> = read_key(conn, "pipeline_default_gates")
        .and_then(|v| serde_json::from_str::<Vec<String>>(&v).ok())
        .unwrap_or_else(|| vec!["review".to_string(), "merge".to_string()]);

    let tracked_labels: Vec<String> = read_key(conn, "pipeline_tracked_labels")
        .and_then(|v| serde_json::from_str::<Vec<String>>(&v).ok())
        .unwrap_or_else(|| vec!["coord".to_string()]);

    let repos: Vec<(String, String)> = read_key(conn, "pipeline_repos")
        .and_then(|v| serde_json::from_str::<serde_json::Value>(&v).ok())
        .and_then(|val| match val {
            serde_json::Value::Object(map) => Some(
                map.into_iter()
                    .filter_map(|(k, v)| v.as_str().map(|s| (k, s.to_string())))
                    .collect(),
            ),
            _ => None,
        })
        .unwrap_or_default();

    let require_plan: bool = read_key(conn, "pipeline_require_plan")
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);

    (default_gates, tracked_labels, repos, require_plan)
}

// ─── CoordApp ─────────────────────────────────────────────────────────────────

/// Backend-neutral coordinator dashboard.
///
/// Implements [`AppLogic`]: all rendering uses the [`Backend`] trait's
/// `draw_*` methods, and event handling maps [`UiEvent`] to state
/// mutations. No ratatui or GTK types appear here.
pub struct CoordApp {
    data: BoardData,
    /// Which top-level view is currently shown in the content area.
    active_view: SidebarView,
    /// SidebarSystem for the Board view — repo sections with issue rows.
    board_sidebar: SidebarSystem,
    /// Ordered list of repo names used as section IDs in the sidebar.
    /// Rebuilt on each data refresh to stay in sync with `board_sidebar`.
    board_repo_names: Vec<String>,
    /// Cached result of `issues_by_repo()`. Rebuilt on `rebuild_board_sidebar()`.
    board_issues_cache: Vec<(String, Vec<IssueGroup>)>,
    /// True when a PROPOSALS section is prepended to the sidebar.
    has_proposals_section: bool,
    /// Selected machine index in the Machines view.
    machine_sel: usize,
    /// Scroll offset for the machines list.
    machine_scroll: usize,
    refreshed_at: Instant,
    /// Scroll offset for the Board Summary detail panel (right side).
    detail_scroll: usize,
    /// Scroll offset for the Machine detail panel.
    machine_detail_scroll: usize,
    /// Background command runner for `coord` CLI subcommands.
    command_runner: crate::commands::CommandRunner,
    /// Last time `coord notify` was auto-triggered.
    last_notify: Instant,
    // ── Issue sync state ─────────────────────────────────────────────────
    /// Last time `coord sync --quiet` was spawned (to rate-limit kicks).
    issue_sync_last: Option<Instant>,
    // ── Board search / status-group state ───────────────────────────────
    /// Current value in the board filter input.
    board_search: String,
    /// Cursor byte offset into `board_search` (always kept at end of value).
    board_search_cursor: usize,
    /// Whether the search input is accepting keyboard input.
    board_search_focused: bool,
    /// Expanded state for each (repo, status_group) pair. Default: true.
    board_status_expanded: std::collections::HashMap<(String, String), bool>,
    // ── Pipeline panel state ────────────────────────────────────────────
    /// SidebarSystem listing tracked issues grouped by repo → lifecycle section.
    pipeline_sidebar: SidebarSystem,
    /// Ordered list of repo keys (coord_repo or repo_slug) used as section IDs
    /// in the pipeline sidebar.  Rebuilt on each `rebuild_pipeline_sidebar()`.
    pipeline_repo_names: Vec<String>,
    /// Tracked issues for the Pipeline panel (loaded asynchronously via gh).
    pipeline_issues: Vec<PipelineIssue>,
    /// Selected issue index into `pipeline_issues`, if any.
    pipeline_sel: Option<usize>,
    /// In-flight `gh search issues` poll (None when idle).
    pipeline_loader: Option<std::sync::mpsc::Receiver<PipelineLoaderResult>>,
    /// When `gh` was last queried — bounds refresh rate.
    pipeline_last_load: Option<Instant>,
    /// Status message shown when a dispatch is queued/skipped due to no
    /// available machine. Cleared after a short TTL.
    pipeline_status: Option<(String, Instant)>,
    /// Active toasts rendered as a bottom-right overlay. Each entry pairs
    /// a `ToastItem` with the time it was added so the host can auto-expire
    /// them after a few seconds without the user dismissing manually.
    toasts: Vec<(ToastItem, Instant, ToastSeverity)>,
    /// Monotonic counter for toast widget IDs (must be unique per toast).
    next_toast_id: u64,
    /// Active watch overlay (live log + kill controls). `None` when no
    /// assignment is being watched.
    watch: Option<WatchState>,
    /// Input text for the inline inject prompt inside the watch overlay.
    /// Empty when not in input mode.
    inject_input: String,
    /// Whether the inject input is currently capturing keyboard input.
    /// Toggled by 'b' (open) and Escape/Enter (close).
    inject_focused: bool,
    /// Which tab is active in the Pipeline detail pane.
    pipeline_detail_tab: PipelineDetailTab,
    /// Which tab is active in the Board detail pane.
    board_detail_tab: BoardDetailTab,
    /// Scroll offset for the issue body on the Issue tab.
    pipeline_detail_scroll: usize,
    /// Cache of remotely-fetched log items, keyed by assignment ID.
    ///
    /// Each entry stores `(fetched_at, items)`. Entries older than 30 s are
    /// re-fetched on the next render that needs them. `RefCell` is used so the
    /// cache can be updated from `&self` methods (render path).
    remote_log_cache: std::cell::RefCell<std::collections::HashMap<String, (Instant, Vec<ListItem>)>>,
    /// Pending background board-data load.  `Some` while a load is in flight;
    /// `None` when idle.  Polled non-blockingly on every [`handle`] call.
    pending_data: Option<std::sync::mpsc::Receiver<BoardData>>,
    /// Most-recent data-load error (message + timestamp), displayed in the
    /// status bar for a short time.  Cleared when the next load succeeds.
    fetch_error: Option<(String, Instant)>,
    /// In-flight remote log fetches keyed by assignment ID.
    ///
    /// Each `Receiver` yields `Ok(raw_content)` or `Err(error_message)`.
    /// `RefCell` allows mutation from `&self` render methods.
    pending_log_fetches: std::cell::RefCell<std::collections::HashMap<String, std::sync::mpsc::Receiver<Result<String, String>>>>,
    /// In-flight `gh issue view` fetches for Board Issue tab bodies that
    /// weren't in the local issues table (e.g. closed >7d ago and pruned).
    /// Keyed by `(repo_name, issue_number)`. The receiver yields `Ok(issue)`
    /// or `Err(error_message)`.
    pending_issue_fetches: std::cell::RefCell<std::collections::HashMap<(String, u64), std::sync::mpsc::Receiver<Result<FetchedIssue, String>>>>,
    /// In-memory cache for successfully-fetched single issues. Survives until
    /// the TUI restarts; the background thread also upserts into the DB so
    /// the next `load_data()` finds it. No TTL — `coord sync` is the source
    /// of truth for invalidation.
    fetched_issues_cache: std::cell::RefCell<std::collections::HashMap<(String, u64), FetchedIssue>>,
    /// Pending purge confirmation state.  `Some((assignments, issues))` means
    /// we are waiting for the user to confirm; `None` means not pending.
    ///
    /// Triggered by pressing 'P' when the Board sidebar selection is in the
    /// Completed (done/merged) group.  Any key other than 'y'/'Y' cancels.
    pending_purge: Option<(usize, usize)>,
    /// #200: inline reason input for Test gate failure. `Some(buf)` means we
    /// are accumulating the reason; Enter submits, Esc cancels. The carried
    /// `usize` is the work-assignment index in `self.data.assignments` that
    /// the verdict will be applied to.
    pending_test_fail: Option<(usize, String)>,
    /// #245: pending `coord merge --force-merge` confirmation.  `Some(repo)`
    /// means the user pressed `m` while the "Checks failed" hint was visible
    /// and we're waiting for one-key confirmation before bypassing the CI
    /// gate.  `repo` is the coord-local repo name to scope the force-merge
    /// to (empty string ⇒ no scope, force-merge the whole queue).  Any key
    /// other than `y`/`Y` cancels; the early-intercept block consumes the
    /// keypress so the normal `y`-as-prefix paths can't fire.
    pending_force_merge: Option<String>,
    /// #259: open right-click context menu, or `None` if no menu is showing.
    /// Opened by right-click on a Board / Pipeline sidebar row; dismissed by
    /// click-outside, Escape, or item activation.
    pending_context_menu: Option<ContextMenuState>,
    /// #259: cached `ContextMenuLayout` from the last render — required for
    /// click hit-testing on the menu items.  Borrowed from the `&self`
    /// render path (same pattern as `settings_form`), populated on every
    /// frame while a menu is open and cleared when it dismisses.
    context_menu_layout: std::cell::RefCell<Option<ContextMenuLayout>>,
    /// Cached visible-row count for the main panel, updated on scroll events
    /// and tick. Used by `watch_log_list` to compute a stick-to-bottom scroll
    /// offset that actually keeps the latest lines on screen (the previous
    /// hard-coded `items.len() - 40` cut off latest lines when the terminal
    /// viewport was under 40 rows).
    last_main_visible_rows: std::cell::Cell<usize>,
    /// Minimum age in days for a done/failed assignment row to be eligible
    /// for the 'P' purge action.  Default 7.
    ///
    /// TODO: wire from coordinator.yml `purge_days` key (e.g. under a top-level
    /// `tui:` section) once the Python config layer supports it.
    purge_days: u32,
    /// Background SSE log-stream state for the watch overlay.
    ///
    /// `Some` while the overlay is open; `None` when closed.  Dropping this
    /// field drops the `Receiver`, which signals the background thread to exit
    /// (it detects the disconnect on the next `tx.send()` call).
    watch_sse: Option<WatchSseState>,

    /// Hover state for the sidebar action bar (above the Board / Pipeline
    /// tree).  Driven by `UiEvent::MouseMoved`; read at render time and
    /// passed to `backend.draw_sidebar_panel` so the rasteriser can tint
    /// the hovered button.  Cleared when the cursor leaves the bar.
    sidebar_action_bar_hover: ToolbarHoverTracker,
    /// Hover state for the view-level panel toolbar (above the main
    /// detail area).  Same shape and lifecycle as
    /// `sidebar_action_bar_hover`.
    panel_toolbar_hover: ToolbarHoverTracker,

    /// Index of the currently-selected stage in the Pipeline > Stages
    /// tab.  `None` defaults to "no stage selected" — first arrow / click
    /// snaps to a sensible default (usually the latest non-pending
    /// stage).  Driven by Left/Right arrows when the Stages tab is
    /// active, and by mouse clicks on stage boxes.  When focused,
    /// quadraui's PipelineView rasteriser draws the box with an accent
    /// border, and coord-tui renders the matching stage's content
    /// (logs / findings) in the scrollable panel below the strip.
    pipeline_focused_stage: Option<usize>,
    /// Scroll offset (line index) for the stage-content scrollable
    /// panel below the pipeline strip.  Reset when the focused stage
    /// changes.
    pipeline_stage_content_scroll: usize,

    // ── Settings panel ───────────────────────────────────────────────────
    /// Persisted user settings loaded from `~/.coord/settings.toml`.
    settings: TuiSettings,
    /// FormController backing the settings form (right pane).
    ///
    /// `RefCell` is used so the form can be rebuilt and rendered from the
    /// `&self` render path (same pattern as `remote_log_cache`).
    settings_form: std::cell::RefCell<FormController>,
    /// Which category row is selected in the settings sidebar (left pane).
    // #237: removed `settings_category_sel` — settings render as one
    // unified scrollable form; there are no categories to select.
    /// Which interactive field is focused within the current category's form.
    /// Reset to 0 when the category changes.
    settings_field_sel: usize,
    /// Assignment IDs that were in `running` state at the most recent data
    /// refresh.  Used to detect running→done/failed transitions so we can
    /// ring the terminal bell when `audio_on_completion` is enabled.
    audio_prev_running: std::collections::HashSet<String>,
    /// #235 Phase 1: in-flight Test-stage build jobs keyed by work_id.
    /// Drained by `poll_test_build_jobs` each tick — completed entries are
    /// removed and surfaced as toasts. Empty in the steady state.
    test_build_jobs: std::collections::HashMap<String, TestBuildJob>,
    /// #271 part 2: persisted outcomes from completed Phase 1 builds.
    /// Keyed by `work_id`.  Drives the persistent "Last build: …" line
    /// in the Pipeline detail panel so the user who missed the 4 s
    /// toast can still see the result.
    last_test_builds: std::collections::HashMap<String, TestBuildResult>,
    /// #271 part 2 follow-up: in-flight `gh pr view` background fetches
    /// keyed by `(repo_slug, pr_number)`.  Drained by
    /// `poll_pending_pr_fetches` each tick; results land in
    /// `fetched_prs_cache`.
    pending_pr_fetches: std::cell::RefCell<
        std::collections::HashMap<(String, i64), std::sync::mpsc::Receiver<Result<FetchedPr, String>>>,
    >,
    /// In-memory cache of the latest `gh pr view` snapshot per PR.
    /// Populated by completed `pending_pr_fetches` rounds; consumed by
    /// the Pipeline detail panel's Test guidance block.
    fetched_prs_cache: std::cell::RefCell<std::collections::HashMap<(String, i64), FetchedPr>>,
    /// #240: CI check summaries for PRs in the merge queue, keyed by
    /// `(repo_github, pr_number)`. Populated by background `gh pr checks`
    /// fetches when the Pipeline view is focused. Cached entries with
    /// `fetched_at.elapsed() > 30s` are refetched on the next kick.
    pipeline_ci_checks: std::collections::HashMap<(String, i64), CiCheckSummary>,
    /// #240: in-flight CI-check fetches keyed by `(repo_github, pr_number)`.
    /// Drained each tick by `poll_ci_check_loaders`.
    pipeline_ci_loader: std::collections::HashMap<
        (String, i64),
        std::sync::mpsc::Receiver<Result<CiCheckSummary, String>>,
    >,
    /// Pipeline issues dismissed from the Done section by the user pressing 'D'.
    ///
    /// Keyed by `(repo_slug, issue_number)`.  Dismissed issues are hidden from
    /// the sidebar for the lifetime of the current session; they reappear on
    /// the next startup (or can be re-fetched if the user clears the set).
    /// This is intentionally in-memory: for MVP, hiding accumulation is
    /// sufficient — no persistence needed.
    pipeline_dismissed: std::collections::HashSet<(String, u64)>,
}

/// Result returned by the background `gh search issues` poll.
enum PipelineLoaderResult {
    /// Successfully parsed issues from gh output.
    Ok(Vec<PipelineIssue>),
    /// gh failed (missing CLI, network error, auth issue, etc.).
    Err(String),
}

impl Default for CoordApp {
    fn default() -> Self {
        Self::new()
    }
}

impl CoordApp {
    /// Create a new app.
    ///
    /// Board data is fetched on a background thread so the UI renders
    /// immediately.  The status bar shows "↻ loading…" until the first
    /// load completes.
    pub fn new() -> Self {
        let mut sidebar = SidebarSystem::new(Vec::new());
        sidebar.set_navigation_mode(NavigationMode::Selection);
        sidebar.set_allow_collapse(true);
        let mut pipeline_sidebar = SidebarSystem::new(Vec::new());
        pipeline_sidebar.set_navigation_mode(NavigationMode::Selection);
        pipeline_sidebar.set_allow_collapse(true);
        let mut app = Self {
            data: BoardData::default(),
            active_view: SidebarView::default(),
            board_sidebar: sidebar,
            board_repo_names: Vec::new(),
            board_issues_cache: Vec::new(),
            has_proposals_section: false,
            machine_sel: 0,
            machine_scroll: 0,
            // Use a far-past instant so the "↻ Xs" counter starts at 0.
            refreshed_at: Instant::now(),
            detail_scroll: 0,
            machine_detail_scroll: 0,
            command_runner: crate::commands::CommandRunner::new(),
            last_notify: Instant::now(),
            issue_sync_last: None,
            board_search: String::new(),
            board_search_cursor: 0,
            board_search_focused: false,
            board_status_expanded: std::collections::HashMap::new(),
            pipeline_sidebar,
            pipeline_repo_names: Vec::new(),
            pipeline_issues: Vec::new(),
            pipeline_sel: None,
            pipeline_loader: None,
            pipeline_last_load: None,
            pipeline_status: None,
            toasts: Vec::new(),
            next_toast_id: 0,
            watch: None,
            inject_input: String::new(),
            inject_focused: false,
            pipeline_detail_tab: PipelineDetailTab::default(),
            board_detail_tab: BoardDetailTab::default(),
            pipeline_detail_scroll: 0,
            remote_log_cache: std::cell::RefCell::new(std::collections::HashMap::new()),
            pending_data: Some(start_data_load()),
            fetch_error: None,
            pending_log_fetches: std::cell::RefCell::new(std::collections::HashMap::new()),
            pending_issue_fetches: std::cell::RefCell::new(std::collections::HashMap::new()),
            fetched_issues_cache: std::cell::RefCell::new(std::collections::HashMap::new()),
            pending_purge: None,
            pending_test_fail: None,
            pending_force_merge: None,
            pending_context_menu: None,
            context_menu_layout: std::cell::RefCell::new(None),
            last_main_visible_rows: std::cell::Cell::new(40),
            purge_days: 7,
            watch_sse: None,
            sidebar_action_bar_hover: ToolbarHoverTracker::new(),
            panel_toolbar_hover: ToolbarHoverTracker::new(),
            pipeline_focused_stage: None,
            pipeline_stage_content_scroll: 0,
            settings: TuiSettings::load(),
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
        };
        app.rebuild_board_sidebar();
        app.rebuild_pipeline_sidebar(None);
        // Sync issues from GitHub on startup so the board backlog is fresh.
        app.kick_issue_sync();
        app
    }

    /// Build the [`ShellConfig`] for the AppShell chrome.
    ///
    /// Three activity-bar panels correspond to the three top-level views.
    /// The status bar is enabled so `render_content()` can draw into
    /// `layout.status_bar_bounds`.
    pub fn shell_config() -> ShellConfig {
        let mut config = ShellConfig::new(
            "coord-tui",
            vec![
                PanelDefinition {
                    id: WidgetId::new("panel:board"),
                    icon: "B".into(),
                    tooltip: "Board".into(),
                    title: "BOARD".into(),
                },
                PanelDefinition {
                    id: WidgetId::new("panel:machines"),
                    icon: "M".into(),
                    tooltip: "Machines".into(),
                    title: "MACHINES".into(),
                },
                PanelDefinition {
                    id: WidgetId::new("panel:pipeline"),
                    // ▶ marks a horizontal play / run pipeline.
                    icon: "▶".into(),
                    tooltip: "Pipeline".into(),
                    title: "PIPELINE".into(),
                },
                PanelDefinition {
                    id: WidgetId::new("panel:settings"),
                    // ⚙ gear icon for settings.
                    icon: "⚙".into(),
                    tooltip: "Settings".into(),
                    title: "SETTINGS".into(),
                },
            ],
        )
        .with_status_bar();
        // Bottom COMMANDS panel removed — it carved sidebar height in half
        // and made the lower sidebar rows fall outside sidebar_content_bounds
        // when many issues were shown. Toasts cover completion notifications;
        // running-command status can move to the status bar in a follow-up.
        config.default_sidebar_width = 35.0;
        config.min_sidebar_width = 20.0;
        config.max_sidebar_width = 55.0;
        config
    }

    /// Kick off a background data load if one is not already in flight.
    fn refresh(&mut self) {
        if self.pending_data.is_none() {
            self.pending_data = Some(start_data_load());
        }
    }

    /// Push a new toast with the given title, body, and severity. Toasts
    /// auto-expire after [`TOAST_TTL`] in [`render`]/[`prune_toasts`].
    #[allow(dead_code)] // used by the watch overlay (#TBD)
    fn push_toast(&mut self, title: &str, body: &str, severity: ToastSeverity) {
        self.next_toast_id += 1;
        let id = WidgetId::new(format!("toast-{}", self.next_toast_id));
        let item = ToastItem {
            id,
            title: title.to_string(),
            body: body.to_string(),
            severity,
            action: None,
            accent: None,
        };
        self.toasts.push((item, Instant::now(), severity));
    }

    /// Drop toasts older than [`TOAST_TTL`] so the overlay stays uncluttered.
    fn prune_toasts(&mut self) {
        self.toasts.retain(|(_, t, _)| t.elapsed() < TOAST_TTL);
    }

    /// Build the visible `ToastStack` for the bottom-right overlay.
    ///
    /// Returns `None` when no toasts are active.  Auto-promotes the
    /// most recent `pipeline_status` message to a toast so every
    /// dispatch site already gets corner feedback without each helper
    /// having to call `push_toast` explicitly.
    fn toast_stack(&self) -> Option<ToastStack> {
        let mut items: Vec<ToastItem> = self
            .toasts
            .iter()
            .map(|(it, _, _)| it.clone())
            .collect();
        if let Some((msg, when)) = &self.pipeline_status {
            if when.elapsed() < TOAST_TTL {
                items.push(ToastItem {
                    id: WidgetId::new(format!("pipeline-status-{}", when.elapsed().as_millis())),
                    title: "Pipeline".to_string(),
                    body: msg.clone(),
                    severity: if msg.contains("no reachable") || msg.contains("no failed") || msg.contains("not found") {
                        ToastSeverity::Warning
                    } else {
                        ToastSeverity::Info
                    },
                    action: None,
                    accent: None,
                });
            }
        }
        if items.is_empty() {
            return None;
        }
        Some(ToastStack {
            id: WidgetId::new("coord-toasts"),
            corner: ToastCorner::BottomRight,
            toasts: items,
        })
    }

    /// Open the watch overlay for the running assignment of the currently
    /// selected Pipeline issue. Falls back to the most recent non-done
    /// assignment when nothing is actively running; pushes a toast and
    /// returns `false` when there's nothing to watch.
    fn open_watch_for_selected_issue(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else { return false; };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else { return false; };
        let local_repo = issue.coord_repo.as_deref();

        let pick = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == r,
                None => true,
            })
            .find(|a| a.status == "running")
            .or_else(|| {
                self.data
                    .assignments
                    .iter()
                    .filter(|a| a.issue_number == issue.number)
                    .filter(|a| match local_repo {
                        Some(r) => a.repo == r,
                        None => true,
                    })
                    .find(|a| a.status != "done")
            });

        match pick {
            Some(a) => {
                self.watch = Some(WatchState {
                    assignment_id: a.id.clone(),
                    machine: a.machine.clone(),
                    repo: a.repo.clone(),
                    issue_number: a.issue_number,
                    assignment_type: a
                        .assignment_type
                        .clone()
                        .unwrap_or_else(|| "work".to_string()),
                    scroll: usize::MAX,
                });
                // Open an SSE log stream for remote machines.
                self.watch_sse = None; // clear any previous
                if let Some(m) = self.data.machines.iter().find(|m| m.name == a.machine) {
                    if !m.host.is_empty() {
                        let rx = spawn_sse_watch(&m.host, &a.id, 0);
                        self.watch_sse = Some(WatchSseState {
                            rx,
                            lines: Vec::new(),
                            last_event_id: 0,
                            fail_count: 0,
                            first_fail_at: None,
                            done: false,
                            host: m.host.clone(),
                            assignment_id: a.id.clone(),
                            pending_tail: String::new(),
                        });
                    }
                }
                true
            }
            None => {
                self.pipeline_status = Some((
                    format!("no assignment to watch for #{}", issue.number),
                    Instant::now(),
                ));
                false
            }
        }
    }

    /// Close the watch overlay and cancel the background SSE thread.
    fn close_watch(&mut self) {
        self.watch = None;
        // Dropping watch_sse drops the Receiver; the background thread detects
        // the disconnect on its next tx.send() and exits cleanly.
        self.watch_sse = None;
    }

    /// Force a fresh SSE connection for the watch overlay (manual refresh, R key).
    ///
    /// Drops the current SSE state (if any) and spawns a new connection from
    /// byte offset 0, so the full log is streamed from the start.
    fn reset_sse_watch(&mut self) {
        let Some(w) = self.watch.as_ref() else { return; };
        let host = match self.data.machines.iter().find(|m| m.name == w.machine) {
            Some(m) if !m.host.is_empty() => m.host.clone(),
            _ => return,
        };
        let id = w.assignment_id.clone();
        let rx = spawn_sse_watch(&host, &id, 0);
        self.watch_sse = Some(WatchSseState {
            rx,
            lines: Vec::new(),
            last_event_id: 0,
            fail_count: 0,
            first_fail_at: None,
            done: false,
            host,
            assignment_id: id,
            pending_tail: String::new(),
        });
    }

    /// Accept the plan being watched: dispatches `coord approve-plan <id>`.
    /// No-op when the watched assignment isn't a plan; toasts otherwise.
    fn approve_watched_plan(&mut self) -> bool {
        let Some(w) = self.watch.clone() else { return false; };
        if w.assignment_type != "plan" {
            self.pipeline_status = Some((
                "approve only works on a plan-type assignment".to_string(),
                Instant::now(),
            ));
            return false;
        }
        let spawned = self
            .command_runner
            .spawn(&["approve-plan", &w.assignment_id]);
        if spawned {
            self.pipeline_status = Some((
                format!("approving plan #{} → dispatching work", w.issue_number),
                Instant::now(),
            ));
            // Close the watch so the user returns to Stages and can see the
            // new work assignment appear on the next refresh.
            self.close_watch();
        } else {
            self.pipeline_status = Some((
                "another command is running — try again in a moment".to_string(),
                Instant::now(),
            ));
        }
        spawned
    }

    /// Find a running or non-done assignment for the currently-selected
    /// Pipeline row and dispatch `coord stop <id>` for it.  Returns
    /// `true` when a stop was dispatched, `false` when no candidate
    /// assignment was found (the caller toasts the user).
    fn dispatch_stop_for_selected_pipeline_row(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else { return false; };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
        let local_repo = issue.coord_repo.as_deref();
        // Prefer a running worker; fall back to any non-done assignment
        // (matches `open_watch_for_selected_issue`'s ordering so
        // Watch and Stop target the same worker).
        let pick = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == r,
                None => true,
            })
            .find(|a| a.status == "running")
            .or_else(|| {
                self.data
                    .assignments
                    .iter()
                    .filter(|a| a.issue_number == issue.number)
                    .filter(|a| match local_repo {
                        Some(r) => a.repo == r,
                        None => true,
                    })
                    .find(|a| a.status != "done")
            });
        let Some(a) = pick else { return false; };
        let aid = a.id.clone();
        let issue_n = a.issue_number;
        let spawned = self.command_runner.spawn(&["stop", &aid]);
        if spawned {
            self.pipeline_status = Some((
                format!("stop dispatched for #{}", issue_n),
                Instant::now(),
            ));
        } else {
            self.pipeline_status = Some((
                "another command is running — try again in a moment".to_string(),
                Instant::now(),
            ));
        }
        spawned
    }

    /// Open the PR for the currently-selected Pipeline row in the user's
    /// default browser via `gh pr view --web`.  Returns `true` when the
    /// child was spawned; `false` when no PR has been opened yet (the
    /// merge_queue entry has no `pr_number`).
    fn dispatch_open_pr_for_selected_pipeline_row(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else { return false; };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
        let Some(pr_number) = self.pipeline_pr_number(&issue) else {
            return false;
        };
        // gh handles xdg-open / open / start cross-platform.  Fire and
        // forget — we don't care about exit code (the user sees the
        // browser open, not a coord-tui status).
        let _ = std::process::Command::new("gh")
            .args([
                "pr", "view",
                &pr_number.to_string(),
                "--repo", &issue.repo_slug,
                "--web",
            ])
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn();
        self.pipeline_status = Some((
            format!("opening PR #{} in browser…", pr_number),
            Instant::now(),
        ));
        true
    }

    /// Stop the assignment being watched: dispatches `coord stop <id>`.
    /// Pushes a toast on success or when another command is running.
    fn kill_watched(&mut self) -> bool {
        let Some(w) = self.watch.clone() else { return false; };
        let spawned = self.command_runner.spawn(&["stop", &w.assignment_id]);
        if spawned {
            self.pipeline_status = Some((
                format!("stop dispatched for #{}", w.issue_number),
                Instant::now(),
            ));
        } else {
            self.pipeline_status = Some((
                "another command is running — try again in a moment".to_string(),
                Instant::now(),
            ));
        }
        spawned
    }

    /// Build the body `ListView` for the watch overlay — the raw log lines
    /// from the worker.  Title carries the repo/issue/machine context.
    /// Inject prompt (when open) is rendered as the last list row.
    ///
    /// Log content is driven by the SSE stream when `watch_sse` is `Some`;
    /// falls back to the polling path (`get_activity_log`) when SSE is
    /// unavailable (e.g. machine host unknown or local assignment).
    fn watch_log_list(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();
        let title = match &self.watch {
            None => " WATCH ".to_string(),
            Some(w) => {
                // Prefer SSE-accumulated lines when available.
                if let Some(sse) = &self.watch_sse {
                    if sse.lines.is_empty() && !sse.done {
                        items.push(kv_item(
                            "",
                            "  Connecting to log stream…",
                            Some(Color::rgb(140, 140, 140)),
                        ));
                    } else {
                        let content = sse.lines.join("\n");
                        items.extend(parse_log_content(&content));
                    }
                    if sse.done {
                        items.push(kv_item(
                            "",
                            "  ── stream ended ──",
                            Some(Color::rgb(90, 90, 90)),
                        ));
                    }
                } else {
                    // Fallback: polling path (local file or remote HTTP GET).
                    items.extend(self.get_activity_log(&w.assignment_id, &w.machine));
                }
                let extra_keys = if w.assignment_type == "plan" {
                    "  A=accept"
                } else {
                    ""
                };
                format!(
                    " WATCH — {} #{} → {} ({}) (b=ask{}  K=kill  R=refresh  q=close) ",
                    w.repo, w.issue_number, w.machine, w.assignment_type, extra_keys
                )
            }
        };
        // If the inject prompt is open, append it as a visible row at the
        // bottom — chrome stays minimal but the user can see what they're
        // typing.
        if self.inject_focused {
            items.push(kv_item("", "", None));
            items.push(ListItem {
                text: StyledText {
                    spans: vec![
                        StyledSpan::with_fg(
                            " ask> ".to_string(),
                            Color::rgb(130, 170, 210),
                        ),
                        StyledSpan::with_fg(
                            format!("{}_", self.inject_input),
                            Color::rgb(255, 255, 255),
                        ),
                    ],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Normal,
            });
            items.push(kv_item(
                "",
                " Enter=send  Esc=cancel ",
                Some(Color::rgb(140, 140, 140)),
            ));
        }
        // Stick-to-bottom default: position the viewport so the LAST item
        // is the last visible row. The viewport-row count is cached during
        // the most recent mouse_main_scroll / tick (see
        // `last_main_visible_rows`); on the very first frame it defaults to
        // 40 so this fits a typical terminal. The hard-coded 40 used to be
        // a literal `items.len() - 40`, which clipped the latest lines on
        // smaller terminals.
        let visible_rows = self.last_main_visible_rows.get().max(1);
        let scroll = self
            .watch
            .as_ref()
            .map(|w| {
                if w.scroll == usize::MAX {
                    items.len().saturating_sub(visible_rows)
                } else {
                    w.scroll
                }
            })
            .unwrap_or(0);
        ListView {
            id: WidgetId::new("watch-log"),
            title: Some(StyledText::plain(&title)),
            items,
            selected_idx: 0,
            scroll_offset: scroll,
            has_focus: false,
            bordered: true,
        }
    }

    /// Submit the current inject input to the watched worker via
    /// `coord inject`.  Closes the input on success.
    fn submit_inject(&mut self) -> bool {
        let Some(w) = self.watch.clone() else { return false; };
        let text = self.inject_input.trim().to_string();
        if text.is_empty() {
            self.inject_focused = false;
            return false;
        }
        let spawned = self
            .command_runner
            .spawn(&["inject", &w.assignment_id, &text]);
        if spawned {
            self.pipeline_status = Some((
                format!("asked worker #{}: {}", w.issue_number, text),
                Instant::now(),
            ));
            self.inject_input.clear();
            self.inject_focused = false;
        } else {
            self.pipeline_status = Some((
                "another command is running — try again in a moment".to_string(),
                Instant::now(),
            ));
        }
        spawned
    }

    /// Spawn `coord sync --quiet` in the background if not already running
    /// and the last sync was more than 5 minutes ago (or never run).
    fn kick_issue_sync(&mut self) {
        const SYNC_INTERVAL: Duration = Duration::from_secs(300);
        if let Some(last) = self.issue_sync_last {
            if last.elapsed() < SYNC_INTERVAL {
                return;
            }
        }
        if self.command_runner.is_running() {
            return;
        }
        if self.command_runner.spawn(&["sync", "--quiet"]) {
            self.issue_sync_last = Some(Instant::now());
        }
    }

    /// Drain any completed background data load, applying results to
    /// `self.data`.  Returns `true` if data was updated (caller should
    /// trigger a redraw).
    fn apply_pending_data(&mut self) -> bool {
        let rx = match &self.pending_data {
            Some(rx) => rx,
            None => return false,
        };
        match rx.try_recv() {
            Ok(data) => {
                // Snapshot which assignments were running before the update so
                // we can detect running→done/failed transitions for the audio bell.
                let prev_running: std::collections::HashSet<String> = self
                    .data
                    .assignments
                    .iter()
                    .filter(|a| a.status == "running")
                    .map(|a| a.id.clone())
                    .collect();

                self.data = data;
                self.pending_data = None;
                self.refreshed_at = Instant::now();
                self.fetch_error = None;
                let m = self.data.machines.len();
                if m > 0 {
                    self.machine_sel = self.machine_sel.min(m - 1);
                } else {
                    self.machine_sel = 0;
                }
                self.rebuild_board_sidebar();
                // apply_pending_data doesn't touch pipeline_issues — the
                // internal capture in rebuild is correct here.  Pass None.
                self.rebuild_pipeline_sidebar(None);

                // Ring the terminal bell (BEL) when an assignment that was
                // running is now done or failed, if the user enabled audio.
                if self.settings.audio_on_completion && !prev_running.is_empty() {
                    let newly_finished = self
                        .data
                        .assignments
                        .iter()
                        .any(|a| {
                            (a.status == "done" || a.status == "failed")
                                && prev_running.contains(&a.id)
                        });
                    if newly_finished {
                        // BEL character — rings the terminal bell or triggers
                        // a system notification depending on terminal settings.
                        eprint!("\x07");
                    }
                }
                self.audio_prev_running = self
                    .data
                    .assignments
                    .iter()
                    .filter(|a| a.status == "running")
                    .map(|a| a.id.clone())
                    .collect();

                true
            }
            Err(std::sync::mpsc::TryRecvError::Empty) => false,
            Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                // Worker thread panicked or dropped sender without sending.
                self.pending_data = None;
                self.fetch_error = Some(("data load failed".into(), Instant::now()));
                true
            }
        }
    }

    /// Group assignments by `(repo, issue_number)`, returning repos in a
    /// stable order (repos with running issues first, then by name).
    fn issues_by_repo(&self) -> Vec<(String, Vec<IssueGroup>)> {
        use std::collections::{BTreeMap, HashMap};

        // Collect unique repos from machines, assignments, and open issues.
        let mut all_repos: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
        for m in &self.data.machines {
            for r in &m.repos {
                all_repos.insert(r.clone());
            }
        }
        for a in &self.data.assignments {
            all_repos.insert(a.repo.clone());
        }
        for oi in &self.data.open_issues {
            all_repos.insert(oi.repo_name.clone());
        }

        // Group assignments by (repo, issue_number).
        let mut repo_issues: HashMap<String, BTreeMap<u64, IssueGroup>> = HashMap::new();
        for a in &self.data.assignments {
            let group = repo_issues
                .entry(a.repo.clone())
                .or_default()
                .entry(a.issue_number)
                .or_insert_with(|| IssueGroup {
                    issue_number: a.issue_number,
                    issue_title: a.issue_title.clone(),
                    assignments: Vec::new(),
                    status_summary: String::new(),
                    is_closed: false,
                    has_open_record: false,
                    labels: Vec::new(),
                });
            group.assignments.push(a.clone());
        }

        // Derive status_summary for each issue group.
        for groups in repo_issues.values_mut() {
            for group in groups.values_mut() {
                // Sort assignments by dispatched_at ascending.
                group.assignments.sort_by(|a, b| {
                    a.dispatched_at
                        .partial_cmp(&b.dispatched_at)
                        .unwrap_or(std::cmp::Ordering::Equal)
                });

                // Check merge queue for this issue.
                let merged = self
                    .data
                    .merge_queue
                    .iter()
                    .any(|e| e.issue_number == Some(group.issue_number) && e.state == "merged");

                if merged && group.assignments.iter().all(|a| a.status == "done") {
                    group.status_summary = "merged".to_string();
                } else if group.assignments.iter().any(|a| a.status == "running") {
                    group.status_summary = "running".to_string();
                } else if group
                    .assignments
                    .last()
                    .map(|a| a.status == "failed")
                    .unwrap_or(false)
                {
                    group.status_summary = "failed".to_string();
                } else if group.assignments.iter().all(|a| a.status == "done") {
                    group.status_summary = "done".to_string();
                } else {
                    group.status_summary = "pending".to_string();
                }
            }
        }

        // #265 / #257 fix: stamp both `is_closed` (cache says closed)
        // and `has_open_record` (cache says open) on every IssueGroup
        // that came from assignments.  Groups whose issue has no cache
        // row stay with both flags `false` — the bucketing treats that
        // as "brain has forgotten about this issue", which is the
        // typical state for historical merged/done assignments whose
        // rows got pruned (the brain only retains closed rows for 7d).
        //
        // #226: also copy the issue's labels onto the group so the
        // Backlog/Refining/Refined classification has data to read.
        for oi in self.data.open_issues.iter() {
            if let Some(groups) = repo_issues.get_mut(&oi.repo_name) {
                if let Some(group) = groups.get_mut(&oi.number) {
                    match oi.state.as_str() {
                        "closed" => group.is_closed = true,
                        "open" => group.has_open_record = true,
                        _ => {}
                    }
                    if group.labels.is_empty() {
                        group.labels = oi.labels.clone();
                    }
                }
            }
        }

        // Inject open issues with no assignment as Pending entries.
        // `data.open_issues` now also carries closed issues (for the Board
        // Issue tab's body lookup), so we must filter to state="open" here
        // or closed issues would appear as Pending rows.
        for oi in self.data.open_issues.iter().filter(|i| i.state == "open") {
            let entry = repo_issues
                .entry(oi.repo_name.clone())
                .or_default()
                .entry(oi.number);
            // Only insert if there's no existing assignment group for this issue.
            entry.or_insert_with(|| IssueGroup {
                issue_number: oi.number,
                issue_title: oi.title.clone(),
                assignments: Vec::new(),
                status_summary: "pending".to_string(),
                is_closed: false,
                has_open_record: true,
                labels: oi.labels.clone(),
            });
        }

        // Build result: each repo with its issues, sorted.
        let mut result: Vec<(String, Vec<IssueGroup>)> = Vec::new();
        for repo in &all_repos {
            let issues = repo_issues
                .remove(repo)
                .map(|m| {
                    let mut v: Vec<IssueGroup> = m.into_values().collect();
                    // Issues with running status first, then by issue number.
                    v.sort_by(|a, b| {
                        let rank = |s: &str| match s {
                            "running" => 0u8,
                            "failed" => 1,
                            "pending" => 2,
                            "done" => 3,
                            "merged" => 4,
                            _ => 5,
                        };
                        rank(&a.status_summary)
                            .cmp(&rank(&b.status_summary))
                            .then_with(|| a.issue_number.cmp(&b.issue_number))
                    });
                    v
                })
                .unwrap_or_default();
            result.push((repo.clone(), issues));
        }

        // Sort repos: those with running/failed issues first.
        result.sort_by(|a, b| {
            let has_active = |issues: &[IssueGroup]| -> u8 {
                if issues.iter().any(|i| i.status_summary == "running") {
                    0
                } else if issues.iter().any(|i| i.status_summary == "failed") {
                    1
                } else if issues.is_empty() {
                    3
                } else {
                    2
                }
            };
            has_active(&a.1).cmp(&has_active(&b.1)).then_with(|| a.0.cmp(&b.0))
        });

        result
    }

    /// Rebuild the SidebarSystem from current data.
    ///
    /// Layout:
    /// - Section 0: search form (always present)
    /// - Section 1: PROPOSALS (only when proposals exist)
    /// - Section 1/2+: one section per repo
    ///
    /// Within each repo section, issues are grouped by status into sub-trees:
    /// Running → Failed → Completed → Pending. Empty groups are omitted.
    /// Rows are filtered by `board_search` (case-insensitive substring).
    fn rebuild_board_sidebar(&mut self) {
        self.board_issues_cache = self.issues_by_repo();
        let grouped = &self.board_issues_cache;

        let prev_selection = self.board_selected_issue();
        let prev_panel_scroll = self.board_sidebar.panel_scroll();

        // Save per-section collapse state keyed by section name.
        let search_offset = 1usize; // always one search section
        let prev_collapsed: std::collections::HashMap<String, bool> = {
            let offset = search_offset + if self.has_proposals_section { 1 } else { 0 };
            let mut map = std::collections::HashMap::new();
            if self.has_proposals_section {
                map.insert(
                    "__proposals__".to_string(),
                    self.board_sidebar.is_collapsed(search_offset),
                );
            }
            let old_names: Vec<String> = self.board_repo_names.clone();
            for (i, name) in old_names.into_iter().enumerate() {
                map.insert(name, self.board_sidebar.is_collapsed(i + offset));
            }
            map
        };

        // #192: the PROPOSALS section is retired.  Right-click → Send
        // to Pipeline (#261) is the canonical path from Refined →
        // Pipeline:New; `coord plan` proposals are a parallel system
        // we no longer surface.  Force the flag false so the section
        // never renders and all the offset arithmetic still adds 0.
        // The `coord plan` CLI keeps working for backwards-compat;
        // its output just doesn't appear in the TUI any more.
        self.has_proposals_section = false;
        let mut defs: Vec<SidebarSectionDef> = Vec::new();

        // Section 0: search/filter form.
        defs.push(SidebarSectionDef::form("board-search", "FILTER"));

        if self.has_proposals_section {
            let mut def = SidebarSectionDef::new("section:proposals".to_string(), "PROPOSALS".to_string());
            def.show_chevron = true;
            def.size = SectionSize::Content;
            defs.push(def);
        }

        for (repo, _) in grouped.iter() {
            let mut def = SidebarSectionDef::new(format!("repo:{}", repo), repo.clone());
            def.show_chevron = true;
            def.size = SectionSize::Content;
            defs.push(def);
        }

        self.board_repo_names = grouped.iter().map(|(r, _)| r.clone()).collect();

        self.board_sidebar = SidebarSystem::new(defs);
        self.board_sidebar.set_navigation_mode(NavigationMode::Selection);
        self.board_sidebar.set_allow_collapse(true);
        self.board_sidebar.set_scroll_mode(ScrollMode::WholePanel);

        // Populate search form (section 0).
        self.board_sidebar.set_form(0, Form {
            id: WidgetId::new("board-search-form"),
            fields: vec![FormField {
                id: WidgetId::new("board-search-input"),
                label: StyledText::plain(""),
                kind: FieldKind::TextInput {
                    value: self.board_search.clone(),
                    placeholder: "Filter issues…".to_string(),
                    cursor: Some(self.board_search_cursor),
                    selection_anchor: None,
                },
                hint: StyledText::plain(""),
                disabled: false,
                validation: None,
            }],
            focused_field: if self.board_search_focused {
                Some(WidgetId::new("board-search-input"))
            } else {
                None
            },
            scroll_offset: 0,
            has_focus: self.board_search_focused,
        });

        let offset = search_offset + if self.has_proposals_section { 1 } else { 0 };

        // Populate PROPOSALS section.
        if self.has_proposals_section {
            let proposal_color = Color::rgb(200, 180, 255);
            let rows: Vec<TreeRow> = self
                .data
                .proposals
                .iter()
                .enumerate()
                .map(|(i, p)| {
                    let text = StyledText {
                        spans: vec![
                            StyledSpan::with_fg(format!("[{}] ", p.id), Color::rgb(180, 180, 220)),
                            StyledSpan::with_fg(format!("{} ", p.machine), Color::rgb(140, 200, 140)),
                            StyledSpan::with_fg(format!("#{} ", p.issue_number), Color::rgb(150, 150, 240)),
                            StyledSpan::plain(trunc(&p.issue_title, 18)),
                        ],
                    };
                    TreeRow {
                        path: vec![i as u16],
                        indent: 0,
                        icon: None,
                        text,
                        badge: Some(Badge::colored(&p.proposal_type, proposal_color)),
                        is_expanded: None,
                        decoration: Decoration::Normal,
                        edit: None,
                    }
                })
                .collect();
            self.board_sidebar.set_rows(search_offset, rows);
            self.board_sidebar.set_section_badge(
                search_offset,
                Some(StyledText::plain(format!("({})", self.data.proposals.len()))),
            );
        }

        // Helper: fuzzy filter — true if the issue matches the search query.
        let query = self.board_search.to_lowercase();

        let issue_matches = |num: u64, title: &str| -> bool {
            if query.is_empty() {
                return true;
            }
            let num_str = num.to_string();
            num_str.contains(&query) || title.to_lowercase().contains(&query)
        };

        // Build per-repo status groups.
        for (cache_idx, (repo, issues)) in grouped.iter().enumerate() {
            let section_idx = cache_idx + offset;

            // #256 / #226 lifecycle model: bucket issues into the
            // five sections.  Classifier must stay in sync with
            // `board_grouped_for_repo` / `select_issue` so click
            // hit-testing and visual rendering agree.
            let mut backlog: Vec<(usize, &IssueGroup)> = Vec::new();
            let mut refining: Vec<(usize, &IssueGroup)> = Vec::new();
            let mut refined: Vec<(usize, &IssueGroup)> = Vec::new();
            let mut in_flight: Vec<(usize, &IssueGroup)> = Vec::new();
            let mut completed: Vec<(usize, &IssueGroup)> = Vec::new();
            for (flat_idx, g) in issues.iter().enumerate() {
                if !issue_matches(g.issue_number, &g.issue_title) {
                    continue;
                }
                match g.lifecycle_section() {
                    "backlog" => backlog.push((flat_idx, g)),
                    "refining" => refining.push((flat_idx, g)),
                    "refined" => refined.push((flat_idx, g)),
                    "in-flight" => in_flight.push((flat_idx, g)),
                    "completed" => completed.push((flat_idx, g)),
                    _ => backlog.push((flat_idx, g)),
                }
            }

            let groups: Vec<(&str, &str, &Vec<(usize, &IssueGroup)>)> = [
                ("Backlog",   "backlog",   &backlog),
                ("Refining",  "refining",  &refining),
                ("Refined",   "refined",   &refined),
                ("In-flight", "in-flight", &in_flight),
                ("Completed", "completed", &completed),
            ]
            .into_iter()
            .filter(|(_, _, v)| !v.is_empty())
            .collect();

            let total: usize = backlog.len()
                + refining.len()
                + refined.len()
                + in_flight.len()
                + completed.len();
            if total > 0 {
                self.board_sidebar.set_section_badge(
                    section_idx,
                    Some(StyledText::plain(format!("({})", total))),
                );
            }

            // Auto-collapse repos with no In-flight work and an empty
            // search.  A user dropping in mid-session cares about
            // running / failed work first; the Pending and Completed
            // backlogs stay one click away behind the chevron.
            let has_active = !in_flight.is_empty();
            if !has_active && total == 0 {
                self.board_sidebar.set_collapsed(section_idx, true);
            }

            let mut rows: Vec<TreeRow> = Vec::new();
            for (group_idx, (display_name, key, group_issues)) in groups.iter().enumerate() {
                let gi = group_idx as u16;
                let is_exp = *self
                    .board_status_expanded
                    .get(&(repo.clone(), key.to_string()))
                    .unwrap_or(&true);

                // #256 / #226 lifecycle palette.  In-flight is the
                // attention-worthy green (running / failed / done-but-
                // open all fold here); Completed is the muted "done"
                // green.  Backlog / Refining / Refined ramp from
                // unscoped grey → warm yellow (active scoping) → cool
                // blue (scope-locked, ready to dispatch).
                let header_color = match *key {
                    "backlog" => Color::rgb(140, 140, 160),
                    "refining" => Color::rgb(220, 180, 100),
                    "refined" => Color::rgb(140, 180, 220),
                    "in-flight" => Color::rgb(80, 220, 80),
                    "completed" => Color::rgb(120, 180, 120),
                    _ => Color::rgb(140, 140, 160),
                };
                rows.push(TreeRow {
                    path: vec![gi],
                    indent: 1,
                    icon: None,
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(
                            format!("{} ({})", display_name, group_issues.len()),
                            header_color,
                        )],
                    },
                    badge: None,
                    is_expanded: Some(is_exp),
                    decoration: Decoration::Header,
                    edit: None,
                });

                if is_exp {
                    for (issue_idx, (_flat_idx, g)) in group_issues.iter().enumerate() {
                        let _sc = g.status_color();
                        let text = StyledText {
                            spans: vec![
                                StyledSpan::with_fg(
                                    format!("#{:<5}", g.issue_number),
                                    Color::rgb(150, 150, 240),
                                ),
                                StyledSpan::plain(trunc(&g.issue_title, 20)),
                            ],
                        };
                        rows.push(TreeRow {
                            path: vec![gi, issue_idx as u16],
                            indent: 2,
                            icon: None,
                            text,
                            badge: None,
                            is_expanded: None,
                            decoration: if g.status_summary == "failed" {
                                Decoration::Error
                            } else {
                                Decoration::Normal
                            },
                            edit: None,
                        });
                    }
                }
            }

            self.board_sidebar.set_rows(section_idx, rows);
        }

        // Activate first non-empty repo section.
        if self.board_sidebar.active_section().is_none() {
            if self.has_proposals_section {
                self.board_sidebar.set_active_section(Some(search_offset));
            } else {
                for (i, (_repo, issues)) in grouped.iter().enumerate() {
                    if !issues.is_empty() {
                        self.board_sidebar.set_active_section(Some(i + offset));
                        break;
                    }
                }
            }
        }

        // Restore previous selection.
        if let Some((prev_repo, prev_issue)) = prev_selection {
            self.select_issue(&prev_repo, prev_issue);
        }

        self.board_sidebar.set_panel_scroll(prev_panel_scroll);

        // Restore collapsed state by section name.
        {
            let new_offset = search_offset + if self.has_proposals_section { 1 } else { 0 };
            if self.has_proposals_section {
                if let Some(&was_collapsed) = prev_collapsed.get("__proposals__") {
                    self.board_sidebar.set_collapsed(search_offset, was_collapsed);
                }
            }
            let new_names: Vec<String> = self.board_repo_names.clone();
            for (i, name) in new_names.into_iter().enumerate() {
                if let Some(&was_collapsed) = prev_collapsed.get(&name) {
                    self.board_sidebar.set_collapsed(i + new_offset, was_collapsed);
                }
            }
        }
    }

    /// Repo section offset: 1 for the search form + 1 more if proposals exist.
    fn board_repo_offset(&self) -> usize {
        1 + if self.has_proposals_section { 1 } else { 0 }
    }

    /// Return the repo name for the active sidebar section, if any.
    fn board_active_repo(&self) -> Option<&str> {
        let section = self.board_sidebar.active_section()?;
        let offset = self.board_repo_offset();
        if section < offset {
            return None;
        }
        self.board_repo_names
            .get(section - offset)
            .map(|s| s.as_str())
    }

    /// Reconstruct the lifecycle groups for a repo using the current
    /// search filter.  Returns `(key, [(flat_idx, &IssueGroup)…])` in
    /// display order: **Backlog → Refining → Refined → In-flight → Completed**.
    ///
    /// Section rules (per #256 / #226 lifecycle model):
    /// - **Backlog**   — open, no assignments, no `status:*` label
    /// - **Refining**  — open, no assignments, `status:refining` label
    /// - **Refined**   — open, no assignments, `status:ready` label
    /// - **In-flight** — open AND has at least one assignment
    /// - **Completed** — closed AND has at least one assignment
    ///
    /// Empty groups are omitted so the sidebar doesn't show stub headers.
    fn board_grouped_for_repo<'a>(
        &'a self,
        issues: &'a [(String, Vec<IssueGroup>)],
        repo: &str,
    ) -> Vec<(&'static str, Vec<(usize, &'a IssueGroup)>)> {
        let (_, flat) = match issues.iter().find(|(r, _)| r == repo) {
            Some(v) => v,
            None => return Vec::new(),
        };
        let query = self.board_search.to_lowercase();
        let mut backlog = Vec::new();
        let mut refining = Vec::new();
        let mut refined = Vec::new();
        let mut in_flight = Vec::new();
        let mut completed = Vec::new();
        for (i, g) in flat.iter().enumerate() {
            if !query.is_empty() {
                let num_str = g.issue_number.to_string();
                if !num_str.contains(&query) && !g.issue_title.to_lowercase().contains(&query) {
                    continue;
                }
            }
            match g.lifecycle_section() {
                "backlog" => backlog.push((i, g)),
                "refining" => refining.push((i, g)),
                "refined" => refined.push((i, g)),
                "in-flight" => in_flight.push((i, g)),
                "completed" => completed.push((i, g)),
                _ => backlog.push((i, g)),
            }
        }
        [
            ("backlog", backlog),
            ("refining", refining),
            ("refined", refined),
            ("in-flight", in_flight),
            ("completed", completed),
        ]
        .into_iter()
        .filter(|(_, v)| !v.is_empty())
        .collect()
    }

    /// Return the IssueGroup currently selected in the board sidebar.
    ///
    /// Paths are now two-level: `[group_idx, issue_idx_within_group]`. A
    /// one-level path (group header selected) returns `None`.
    fn board_selected_issue_group(&self) -> Option<&IssueGroup> {
        let section = self.board_sidebar.active_section()?;
        let offset = self.board_repo_offset();
        if section < offset {
            return None;
        }
        let path = self.board_sidebar.selected_path(section)?;
        if path.len() < 2 {
            return None;
        }
        let group_idx = path[0] as usize;
        let issue_idx = path[1] as usize;
        let repo = self.board_repo_names.get(section - offset)?;
        let groups = self.board_grouped_for_repo(&self.board_issues_cache, repo);
        let (_, issues_in_group) = groups.get(group_idx)?;
        let (flat_idx, _) = issues_in_group.get(issue_idx)?;
        let (_, all_issues) = self.board_issues_cache.iter().find(|(r, _)| r == repo)?;
        all_issues.get(*flat_idx)
    }

    /// Return the (repo, issue_number) currently selected in the board sidebar.
    fn board_selected_issue(&self) -> Option<(String, u64)> {
        let group = self.board_selected_issue_group()?;
        let repo = self.board_active_repo()?;
        Some((repo.to_string(), group.issue_number))
    }

    /// Return the Proposal currently selected in the sidebar's PROPOSALS section.
    fn board_selected_proposal(&self) -> Option<&Proposal> {
        if !self.has_proposals_section {
            return None;
        }
        let section = self.board_sidebar.active_section()?;
        // Proposals section is at index 1 (after the search form).
        if section != 1 {
            return None;
        }
        let path = self.board_sidebar.selected_path(1)?;
        if path.is_empty() {
            return None;
        }
        self.data.proposals.get(path[0] as usize)
    }

    /// Return the failed Assignment currently selected in the board sidebar.
    fn board_selected_failed_assignment(&self) -> Option<&Assignment> {
        let group = self.board_selected_issue_group()?;
        let failed = group
            .assignments
            .iter()
            .find(|a| a.status == "failed")?;
        Some(failed)
    }

    /// Return `true` when the currently selected row in the Board sidebar is
    /// within the "Completed" (done/merged) status group.
    ///
    /// Used as the guard condition for the 'P' purge keybind: the action is
    /// only available when the user has navigated into the Done section,
    /// preventing accidental purges from other views.
    fn board_selection_in_completed_group(&self) -> bool {
        let section = match self.board_sidebar.active_section() {
            Some(s) => s,
            None => return false,
        };
        let offset = self.board_repo_offset();
        if section < offset {
            return false;
        }
        let path = match self.board_sidebar.selected_path(section) {
            Some(p) => p,
            None => return false,
        };
        if path.is_empty() {
            return false;
        }
        let group_idx = path[0] as usize;
        let repo = match self.board_repo_names.get(section - offset) {
            Some(r) => r,
            None => return false,
        };
        let groups = self.board_grouped_for_repo(&self.board_issues_cache, repo);
        matches!(groups.get(group_idx), Some(("completed", _)))
    }

    /// Try to select a specific issue in the sidebar by repo and issue number.
    fn select_issue(&mut self, repo: &str, issue_number: u64) {
        let offset = self.board_repo_offset();
        // Find the repo's cache entry and its groups.
        let cache_idx = match self.board_repo_names.iter().position(|r| r == repo) {
            Some(i) => i,
            None => return,
        };
        let section_idx = cache_idx + offset;
        // Reconstruct groups to find the 2-level path for this issue.
        // Clone the flat list to avoid borrow conflicts.
        let flat: Vec<IssueGroup> = match self.board_issues_cache.iter().find(|(r, _)| r == repo) {
            Some((_, v)) => v.clone(),
            None => return,
        };
        // #256 / #226 lifecycle bucketing — keep in sync with
        // `board_grouped_for_repo` and `rebuild_board_sidebar` so a
        // `select_issue` lookup lands on the same `[group, issue]`
        // path the click handler resolves to.
        let query = self.board_search.to_lowercase();
        let mut backlog: Vec<(usize, u64)> = Vec::new();
        let mut refining: Vec<(usize, u64)> = Vec::new();
        let mut refined: Vec<(usize, u64)> = Vec::new();
        let mut in_flight: Vec<(usize, u64)> = Vec::new();
        let mut completed: Vec<(usize, u64)> = Vec::new();
        for (i, g) in flat.iter().enumerate() {
            if !query.is_empty() {
                let num_str = g.issue_number.to_string();
                if !num_str.contains(&query) && !g.issue_title.to_lowercase().contains(&query) {
                    continue;
                }
            }
            match g.lifecycle_section() {
                "backlog" => backlog.push((i, g.issue_number)),
                "refining" => refining.push((i, g.issue_number)),
                "refined" => refined.push((i, g.issue_number)),
                "in-flight" => in_flight.push((i, g.issue_number)),
                "completed" => completed.push((i, g.issue_number)),
                _ => backlog.push((i, g.issue_number)),
            }
        }
        let groups_ordered: Vec<Vec<(usize, u64)>> =
            [backlog, refining, refined, in_flight, completed]
                .into_iter()
                .filter(|v| !v.is_empty())
                .collect();
        for (group_idx, group_issues) in groups_ordered.iter().enumerate() {
            for (issue_idx, (_flat_idx, num)) in group_issues.iter().enumerate() {
                if *num == issue_number {
                    self.board_sidebar.set_active_section(Some(section_idx));
                    self.board_sidebar.set_selected_path(
                        section_idx,
                        Some(vec![group_idx as u16, issue_idx as u16]),
                    );
                    return;
                }
            }
        }
    }

    /// Clamp `machine_scroll` so that `machine_sel` is inside the visible window.
    fn fix_machine_scroll(&mut self, visible: usize) {
        if visible == 0 {
            return;
        }
        if self.machine_sel < self.machine_scroll {
            self.machine_scroll = self.machine_sel;
        } else if self.machine_sel >= self.machine_scroll + visible {
            self.machine_scroll = self.machine_sel + 1 - visible;
        }
    }

    // ── Widget builders ──────────────────────────────────────────────────

    fn machines_list(&self, has_focus: bool) -> ListView {
        let items: Vec<ListItem> = self
            .data
            .machines
            .iter()
            .map(|m| {
                let (col, bullet) = if m.reachable {
                    (Color::rgb(70, 210, 70), "● ")
                } else {
                    (Color::rgb(90, 90, 90), "○ ")
                };
                let is_local = m.name == self.data.local_machine;
                let display_name = if is_local {
                    format!("{} (local)", trunc(&m.name, 13))
                } else {
                    trunc(&m.name, 20).to_string()
                };
                let text = StyledText {
                    spans: vec![
                        StyledSpan::with_fg(bullet, col),
                        StyledSpan::plain(&display_name),
                    ],
                };
                let (active_str, active_col) = if m.active_count > 0 {
                    (
                        format!("{} active", m.active_count),
                        Color::rgb(80, 210, 80),
                    )
                } else {
                    ("idle".to_string(), Color::rgb(90, 90, 90))
                };
                let detail = Some(StyledText {
                    spans: vec![StyledSpan::with_fg(&active_str, active_col)],
                });
                ListItem {
                    text,
                    icon: None,
                    detail,
                    decoration: Decoration::Normal,
                }
            })
            .collect();

        let n = self.data.machines.len();
        ListView {
            id: WidgetId::new("machines"),
            title: Some(StyledText::plain(format!(" MACHINES ({}) ", n))),
            items,
            selected_idx: if n > 0 { self.machine_sel } else { 0 },
            scroll_offset: self.machine_scroll,
            has_focus,
            bordered: false,
        }
    }

    fn detail_list(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();

        // If a proposal is selected, show proposal detail instead.
        if let Some(p) = self.board_selected_proposal() {
            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(
                        format!(" Proposal #{} ", p.id),
                        Color::rgb(210, 220, 255),
                    )],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Header,
            });
            items.push(kv_item("  Machine", &format!("  {}", p.machine), None));
            items.push(kv_item("  Repo", &format!("  {}", p.repo), None));
            items.push(kv_item("  Issue", &format!("  #{}: {}", p.issue_number, p.issue_title), None));
            items.push(kv_item("  Type", &format!("  {}", p.proposal_type), None));
            items.push(kv_item("", "", None));
            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(
                        " RATIONALE ",
                        Color::rgb(130, 130, 150),
                    )],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Header,
            });
            for line in p.rationale.lines() {
                items.push(kv_item("", &format!("  {}", line), None));
            }
            items.push(kv_item("", "", None));
            items.push(kv_item("", "  a=approve  A=approve all", Some(Color::rgb(180, 180, 120))));
            return ListView {
                id: WidgetId::new("detail"),
                title: Some(StyledText::plain(&format!("Proposal #{}", p.id))),
                items,
                selected_idx: 0,
                scroll_offset: self.detail_scroll,
                has_focus: false,
                bordered: false,
            };
        }

        match self.board_selected_issue_group() {
            None => {
                items.push(kv_item("", " No issue selected", None));
            }
            Some(group) => {
                let repo = self.board_active_repo().unwrap_or("?");

                // Section header
                let header_text = format!(" {} #{} ", repo, group.issue_number);
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(
                            &header_text,
                            Color::rgb(210, 220, 255),
                        )],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                });

                // Issue title
                items.push(kv_item("", &format!("  {}", trunc(&group.issue_title, 52)), None));
                items.push(kv_item("", "", None)); // blank separator

                // Pipeline stages sub-header
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(
                            " PIPELINE STAGES ",
                            Color::rgb(130, 130, 150),
                        )],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                });

                // Show actual pipeline stages from assignments (ordered by dispatched_at).
                for a in &group.assignments {
                    let type_label = match a.assignment_type.as_deref() {
                        Some("review") => "Review",
                        Some("smoke") => "Smoke",
                        Some("plan") => "Plan",
                        _ => "Work",
                    };
                    items.push(pipeline_stage_item(type_label, Some(a)));
                }

                // PR/Merge stage from merge_queue (only if entry exists).
                if let Some(mq_entry) = self
                    .data
                    .merge_queue
                    .iter()
                    .find(|e| e.issue_number == Some(group.issue_number))
                {
                    items.push(pipeline_merge_item(Some(mq_entry)));
                }

                items.push(kv_item("", "", None)); // blank separator

                // Per-assignment detail section
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(
                            " ASSIGNMENTS ",
                            Color::rgb(130, 130, 150),
                        )],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                });

                for a in &group.assignments {
                    let sc = a.status_color();
                    let type_label = a.assignment_type.as_deref().unwrap_or("work");
                    let text = StyledText {
                        spans: vec![
                            StyledSpan::with_fg(
                                format!("  {:<8}", type_label),
                                Color::rgb(180, 180, 200),
                            ),
                            StyledSpan::with_fg(a.status_label(), sc),
                            StyledSpan::with_fg(
                                format!("  {}", trunc(&a.machine, 15)),
                                Color::rgb(160, 160, 160),
                            ),
                            StyledSpan::with_fg(
                                format!("  {}", a.age_str()),
                                Color::rgb(100, 100, 100),
                            ),
                        ],
                    };
                    items.push(ListItem {
                        text,
                        icon: None,
                        detail: Some(StyledText {
                            spans: vec![StyledSpan::with_fg(
                                trunc(&a.id, 8),
                                Color::rgb(100, 100, 120),
                            )],
                        }),
                        decoration: if a.status == "failed" {
                            Decoration::Error
                        } else {
                            Decoration::Normal
                        },
                    });

                    // Show branch if present
                    if let Some(b) = &a.branch {
                        items.push(kv_item("    Branch", trunc(b, 40), None));
                    }
                    if let Some(m) = &a.model {
                        items.push(kv_item("    Model", trunc(m, 30), None));
                    }
                    if let Some(code) = a.exit_code {
                        let (s, c) = if code == 0 {
                            (format!("{} (ok)", code), Some(Color::rgb(80, 210, 80)))
                        } else {
                            (format!("{} (err)", code), Some(Color::rgb(210, 70, 70)))
                        };
                        items.push(kv_item("    Exit", &s, c));
                    }
                    if let (Some(start), Some(end)) = (a.dispatched_at, a.finished_at) {
                        let dur = (end - start).max(0.0) as u64;
                        items.push(kv_item("    Duration", &fmt_dur(dur), None));
                    }
                }
            }
        }

        let title = match self.board_selected_issue_group() {
            Some(group) => {
                let repo = self.board_active_repo().unwrap_or("?");
                format!(" {} #{} ", repo, group.issue_number)
            }
            None => " DETAIL ".to_string(),
        };

        ListView {
            id: WidgetId::new("detail"),
            title: Some(StyledText::plain(&title)),
            items,
            selected_idx: 0,
            scroll_offset: self.detail_scroll,
            has_focus: false,
            bordered: false,
        }
    }

    /// Return log items for `id`, reading locally or fetching from the remote agent.
    ///
    /// This method **never blocks** the UI thread:
    ///
    /// 1. If a local log file exists, parse and return it immediately.
    /// 2. Drain any completed background fetch from `pending_log_fetches`; on
    ///    success update `remote_log_cache` and return the parsed items.
    /// 3. Return cached items if the 30-second TTL has not expired.
    /// 4. If a fetch is already in flight, return a "Loading log…" placeholder.
    /// 5. Otherwise spawn a background fetch (via [`spawn_log_fetch`]) and
    ///    return a "Loading log…" placeholder; the result will be picked up on
    ///    the next render pass.
    fn get_activity_log(&self, id: &str, machine_name: &str) -> Vec<ListItem> {
        // 1. Local file takes priority — fast path, no network involved.
        let path = coord_dir().join("logs").join(format!("{}.log", id));
        if path.exists() {
            return load_activity_log(id);
        }

        // 2. Drain any completed background fetch for this ID.
        //    We borrow, call try_recv(), then drop the borrow before touching
        //    `remote_log_cache` to avoid a double-borrow panic.
        let pending_result = {
            let pending = self.pending_log_fetches.borrow();
            pending.get(id).map(|rx| rx.try_recv())
        };
        if let Some(recv_result) = pending_result {
            match recv_result {
                Ok(fetch_result) => {
                    self.pending_log_fetches.borrow_mut().remove(id);
                    let items = match fetch_result {
                        Ok(content) => parse_log_content(&content),
                        Err(e) => vec![kv_item(
                            "",
                            &format!("  Log unavailable: {}", e),
                            Some(Color::rgb(100, 100, 100)),
                        )],
                    };
                    self.remote_log_cache
                        .borrow_mut()
                        .insert(id.to_string(), (Instant::now(), items.clone()));
                    return items;
                }
                Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                    // Thread died without sending; remove so we retry below.
                    self.pending_log_fetches.borrow_mut().remove(id);
                }
                Err(std::sync::mpsc::TryRecvError::Empty) => {} // still in flight
            }
        }

        // 3. Cache hit (within the configured TTL). Short TTL makes the Watch
        // overlay feel like tail -f rather than a periodic poll. TTL is
        // user-configurable in the Settings panel (default 2 s).
        {
            let cache = self.remote_log_cache.borrow();
            if let Some((fetched_at, items)) = cache.get(id) {
                if fetched_at.elapsed() < self.settings.log_cache_ttl.as_duration() {
                    return items.clone();
                }
            }
        }

        // 4. Fetch still in flight — return placeholder so the render doesn't block.
        if self.pending_log_fetches.borrow().contains_key(id) {
            return vec![kv_item("", "  Loading log…", Some(Color::rgb(140, 140, 140)))];
        }

        // 5. Cache cold/stale and no fetch in flight. Look up host and spawn.
        let host = match self.data.machines.iter().find(|m| m.name == machine_name) {
            Some(m) if !m.host.is_empty() => m.host.clone(),
            _ => {
                return vec![kv_item(
                    "",
                    "  Log unavailable: machine host unknown",
                    Some(Color::rgb(100, 100, 100)),
                )];
            }
        };
        let rx = spawn_log_fetch(&host, id);
        self.pending_log_fetches.borrow_mut().insert(id.to_string(), rx);
        vec![kv_item("", "  Loading log…", Some(Color::rgb(140, 140, 140)))]
    }

    /// Detail panel for the selected machine: status + job history.
    fn machine_detail_list(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();

        match self.data.machines.get(self.machine_sel) {
            None => {
                items.push(kv_item("", " No machine selected", None));
            }
            Some(m) => {
                // Section header
                let header_text = format!(" {} ", m.name);
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(&header_text, Color::rgb(210, 220, 255))],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                });

                items.push(kv_item("", "", None)); // blank

                let (reach_str, reach_col) = if m.reachable {
                    ("reachable", Color::rgb(80, 210, 80))
                } else {
                    ("unreachable", Color::rgb(220, 70, 70))
                };
                items.push(kv_item("Status", reach_str, Some(reach_col)));

                let is_local = m.name == self.data.local_machine;
                if is_local {
                    items.push(kv_item(
                        "Location",
                        "local",
                        Some(Color::rgb(100, 180, 240)),
                    ));
                }

                let (active_str, active_col) = if m.active_count > 0 {
                    (
                        format!("{} running", m.active_count),
                        Color::rgb(80, 210, 80),
                    )
                } else {
                    ("idle".to_string(), Color::rgb(90, 90, 90))
                };
                items.push(kv_item("Jobs", &active_str, Some(active_col)));

                items.push(kv_item("", "", None)); // blank

                // Job history sub-header
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(
                            " JOB HISTORY ",
                            Color::rgb(130, 130, 150),
                        )],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                });

                let machine_jobs: Vec<&Assignment> = self
                    .data
                    .assignments
                    .iter()
                    .filter(|a| a.machine == m.name)
                    .take(20)
                    .collect();

                if machine_jobs.is_empty() {
                    items.push(kv_item("", "  No jobs found", None));
                } else {
                    for a in machine_jobs {
                        let sc = a.status_color();
                        let issue = format!("  #{:<5}", a.issue_number);
                        let repo = format!("{:<15}", trunc(&a.repo, 15));
                        let st = a.status_label();
                        let text = StyledText {
                            spans: vec![
                                StyledSpan::with_fg(&issue, Color::rgb(150, 150, 240)),
                                StyledSpan::plain(&repo),
                                StyledSpan::with_fg(st, sc),
                            ],
                        };
                        let detail = Some(StyledText {
                            spans: vec![StyledSpan::with_fg(
                                a.age_str(),
                                Color::rgb(100, 100, 100),
                            )],
                        });
                        items.push(ListItem {
                            text,
                            icon: None,
                            detail,
                            decoration: if a.status == "failed" {
                                Decoration::Error
                            } else {
                                Decoration::Normal
                            },
                        });
                    }
                }
            }
        }

        let title = match self.data.machines.get(self.machine_sel) {
            Some(m) => format!(" DETAIL — {} ", m.name),
            None => " DETAIL ".to_string(),
        };

        ListView {
            id: WidgetId::new("machine-detail"),
            title: Some(StyledText::plain(&title)),
            items,
            selected_idx: 0,
            scroll_offset: self.machine_detail_scroll,
            has_focus: false,
            bordered: false,
        }
    }


    // ── Pipeline panel ────────────────────────────────────────────────────

    /// Effective list of stages: a Plan stage (when `pipeline_require_plan`
    /// is set), then "work", then the configured `pipeline.default_gates`
    /// (deduplicated to handle accidental "work" / "plan" entries in the
    /// gate list).
    fn pipeline_stage_names(&self) -> Vec<String> {
        let mut stages: Vec<String> = Vec::with_capacity(4);
        if self.data.pipeline_require_plan {
            stages.push("plan".to_string());
        }
        stages.push("work".to_string());
        for g in &self.data.pipeline_default_gates {
            if g != "work" && g != "plan" {
                stages.push(g.clone());
            }
        }
        stages
    }

    /// Per-issue stage list — prepends a "plan" stage when the issue has
    /// at least one `type="plan"` assignment, even if `pipeline_require_plan`
    /// is false globally.
    ///
    /// Motivation: the user can right-click → Start with Plan on a
    /// per-issue basis (#262).  Without this override, the plan-typed
    /// assignment would get folded into the Work stage (turning it
    /// green) and the user would see no evidence Plan ever ran.
    fn pipeline_stage_names_for_issue(&self, issue: &PipelineIssue) -> Vec<String> {
        let mut stages = self.pipeline_stage_names();
        let already_has_plan = stages.first().map(|s| s == "plan").unwrap_or(false);
        if !already_has_plan && self.issue_has_plan_assignment(issue) {
            stages.insert(0, "plan".to_string());
        }
        stages
    }

    /// True iff at least one assignment with `type="plan"` exists for
    /// *issue* (matching by issue number and, when set, coord_repo).
    fn issue_has_plan_assignment(&self, issue: &PipelineIssue) -> bool {
        self.data.assignments.iter().any(|a| {
            a.assignment_type.as_deref() == Some("plan")
                && a.issue_number == issue.number
                && issue
                    .coord_repo
                    .as_deref()
                    .map(|r| r == a.repo)
                    .unwrap_or(true)
        })
    }

    /// Compute the lifecycle section key for a pipeline issue.
    ///
    /// Priority (highest wins):
    /// 1. `is_closed`            → "done"
    /// 2. Has any assignment     → "in-progress"
    /// 3. Has label `status:ready` (no assignments) → "pending"
    /// 4. Has label `status:refining`               → "refining"
    /// 5. Otherwise                                 → "new"
    fn pipeline_lifecycle_section(&self, issue: &PipelineIssue) -> &'static str {
        if issue.is_closed {
            return "done";
        }
        let has_assignments = self.data.assignments.iter().any(|a| {
            a.issue_number == issue.number
                && issue
                    .coord_repo
                    .as_deref()
                    .map(|r| r == a.repo)
                    .unwrap_or(true)
        });
        if has_assignments {
            return "in-progress";
        }
        if issue.all_labels.iter().any(|l| l == "status:ready") {
            return "pending";
        }
        if issue.all_labels.iter().any(|l| l == "status:refining") {
            return "refining";
        }
        "new"
    }

    /// Return true when any assignment in the DB touches this issue.
    ///
    /// Used to distinguish "closed via coord pipeline" (has assignment rows) from
    /// "closed externally / implemented in-session" (zero assignment rows ever
    /// created).  Matches by issue number and coord repo (or repo_slug when no
    /// local mapping exists).
    fn issue_has_any_assignment(&self, issue: &PipelineIssue) -> bool {
        self.data.assignments.iter().any(|a| {
            a.issue_number == issue.number
                && if let Some(local) = &issue.coord_repo {
                    a.repo == *local
                } else {
                    true
                }
        })
    }

    /// Return the coord_repo name for an issue, falling back to the repo_slug.
    fn pipeline_repo_key(issue: &PipelineIssue) -> &str {
        issue.coord_repo.as_deref().unwrap_or(&issue.repo_slug)
    }

    /// Group pipeline issues under a repo into non-empty lifecycle sections.
    ///
    /// #225 / #256: the Pipeline panel only displays the three sections
    /// that actually carry pipeline state — **New** (`coord +
    /// status:ready`, no assignments), **In-progress**, and **Done**.
    /// Pre-pipeline states (`backlog` = no `status:*` label / `refining`
    /// = `status:refining`) live on the Board's sidebar; the Pipeline
    /// hides them so the panel stays focused on execution.
    ///
    /// The internal classifier key for "New" is still the legacy
    /// `"pending"` — renaming the key would churn every consumer; the
    /// display label is handled in `LIFECYCLE_META` instead.
    ///
    /// Returns `(lifecycle_key, Vec<pipeline_issues index>)` in display
    /// order (New → In-progress → Done), skipping empty sections.
    /// Issues that the user has dismissed via 'D' are excluded.
    fn pipeline_groups_for_repo(&self, repo_key: &str) -> Vec<(&'static str, Vec<usize>)> {
        const VISIBLE_LIFECYCLE: [&str; 3] = ["pending", "in-progress", "done"];
        let mut result: Vec<(&'static str, Vec<usize>)> = Vec::new();
        for &lc in &VISIBLE_LIFECYCLE {
            let idxs: Vec<usize> = self
                .pipeline_issues
                .iter()
                .enumerate()
                .filter(|(_, issue)| {
                    Self::pipeline_repo_key(issue) == repo_key
                        && self.pipeline_lifecycle_section(issue) == lc
                        && !self
                            .pipeline_dismissed
                            .contains(&(issue.repo_slug.clone(), issue.number))
                })
                .map(|(i, _)| i)
                .collect();
            if !idxs.is_empty() {
                result.push((lc, idxs));
            }
        }
        result
    }

    /// Capture the (repo_slug, issue_number) of the currently-selected
    /// pipeline issue.  Callers that are about to replace
    /// `self.pipeline_issues` MUST call this first, then pass the result
    /// into `rebuild_pipeline_sidebar` — otherwise the rebuild's own
    /// internal lookup reads the stale `pipeline_sel` index against the
    /// fresh `pipeline_issues` list and gets either the wrong issue or
    /// `None`, defaulting the selection to issue #0.  That's the bug
    /// behind "the 15-second refresh keeps jumping me back to the top".
    fn capture_pipeline_selection_id(&self) -> Option<(String, u64)> {
        self.pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))
            .map(|i| (i.repo_slug.clone(), i.number))
    }

    /// Build the SidebarSystem entries for the Pipeline panel.
    ///
    /// One section per repo; within each repo, issues are bucketed into
    /// five lifecycle sub-groups (New → Refining → Pending → In-progress →
    /// Done).  Empty sub-groups collapse automatically.  Re-runs after every
    /// successful `gh` poll.
    ///
    /// `prev_sel_override` carries the (repo_slug, issue#) of the
    /// previously-selected issue when the caller has just replaced
    /// `self.pipeline_issues` (and thus the internal capture below would
    /// read garbage).  When `None`, the function captures from the current
    /// in-memory state — that's correct for callers that haven't swapped
    /// `pipeline_issues`.  See [`capture_pipeline_selection_id`].
    fn rebuild_pipeline_sidebar(
        &mut self,
        prev_sel_override: Option<(String, u64)>,
    ) {
        // Preserve selection across rebuilds by (repo_slug, issue#).  Use
        // the caller-provided value when available (it captured before any
        // pipeline_issues swap) and only fall back to the internal capture
        // when the caller didn't touch the list.
        let prev_sel = prev_sel_override.or_else(|| self.capture_pipeline_selection_id());
        // Preserve panel scroll across rebuilds — without this, every 15 s
        // refresh resets the sidebar's scroll to 0 and yanks the visible
        // area back to the top, even when the selection itself is restored
        // correctly. The user wants "the view should not change on refresh".
        let prev_panel_scroll = self.pipeline_sidebar.panel_scroll();
        // Preserve per-section collapse state by repo name (section indices
        // may shift if repos are added/removed between rebuilds, so we key
        // by the repo identifier rather than the section index).  Mirrors
        // the pattern already used by rebuild_board_sidebar.
        let prev_collapsed: std::collections::HashMap<String, bool> = self
            .pipeline_repo_names
            .iter()
            .enumerate()
            .map(|(i, name)| (name.clone(), self.pipeline_sidebar.is_collapsed(i)))
            .collect();

        // Collect unique repo keys in stable order (issues are already sorted
        // by repo_slug within fetch_pipeline_issues).
        let mut repos: Vec<String> = Vec::new();
        for issue in &self.pipeline_issues {
            let key = Self::pipeline_repo_key(issue).to_string();
            if !repos.contains(&key) {
                repos.push(key);
            }
        }

        // One section per repo.
        let mut defs: Vec<SidebarSectionDef> = Vec::new();
        for repo in &repos {
            let mut def =
                SidebarSectionDef::new(format!("section:repo:{}", repo), repo.clone());
            def.show_chevron = true;
            def.size = SectionSize::Content;
            defs.push(def);
        }

        let mut sidebar = SidebarSystem::new(defs);
        sidebar.set_navigation_mode(NavigationMode::Selection);
        sidebar.set_allow_collapse(true);
        sidebar.set_scroll_mode(ScrollMode::WholePanel);

        // Lifecycle display labels.  #225 + #256: Pipeline only renders
        // three sections (New / In-progress / Done).  The classifier
        // key `"pending"` corresponds to the umbrella's "New" — keeping
        // the key avoids churning every consumer for a cosmetic rename;
        // the display label here is what the user actually sees.
        const LIFECYCLE_META: [(&str, &str); 3] = [
            ("pending",     "New"),
            ("in-progress", "In-progress"),
            ("done",        "Done"),
        ];

        // Populate rows for each repo section.
        for (sec_idx, repo_key) in repos.iter().enumerate() {
            let groups = self.pipeline_groups_for_repo(repo_key);
            let total: usize = groups.iter().map(|(_, v)| v.len()).sum();
            if total > 0 {
                sidebar.set_section_badge(
                    sec_idx,
                    Some(StyledText::plain(format!("({})", total))),
                );
            }

            let mut rows: Vec<TreeRow> = Vec::new();
            for (li, (lc_key, issue_idxs)) in groups.iter().enumerate() {
                // Find the display label for this lifecycle key.
                let lc_label = LIFECYCLE_META
                    .iter()
                    .find(|(k, _)| k == lc_key)
                    .map(|(_, v)| *v)
                    .unwrap_or(lc_key);

                // #225 / #256: only three sections visible.  Palette
                // matches the Board's In-flight / Completed / Refined
                // colours so the same lifecycle stage looks consistent
                // across the two panels.
                let header_color = match *lc_key {
                    "in-progress" => Color::rgb(80, 220, 80),
                    "done"        => Color::rgb(120, 180, 120),
                    "pending"     => Color::rgb(140, 180, 240), // "New"
                    _             => Color::rgb(140, 140, 160),
                };

                rows.push(TreeRow {
                    path: vec![li as u16],
                    indent: 1,
                    icon: None,
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(
                            format!("{} ({})", lc_label, issue_idxs.len()),
                            header_color,
                        )],
                    },
                    badge: None,
                    is_expanded: Some(true),
                    decoration: Decoration::Header,
                    edit: None,
                });

                for (ii, &issue_idx) in issue_idxs.iter().enumerate() {
                    let issue = &self.pipeline_issues[issue_idx];
                    let stage_name = self.derive_current_stage(issue);
                    let (badge_text, badge_color) = stage_badge(&stage_name);
                    let title_color = if issue.coord_repo.is_some() {
                        Color::rgb(210, 210, 210)
                    } else {
                        Color::rgb(140, 140, 140)
                    };
                    let text = StyledText {
                        spans: vec![
                            StyledSpan::with_fg(
                                format!("#{:<5}", issue.number),
                                Color::rgb(150, 150, 240),
                            ),
                            StyledSpan::with_fg(trunc(&issue.title, 20), title_color),
                        ],
                    };
                    rows.push(TreeRow {
                        path: vec![li as u16, ii as u16],
                        indent: 2,
                        icon: None,
                        text,
                        badge: Some(Badge::colored(&badge_text, badge_color)),
                        is_expanded: None,
                        decoration: Decoration::Normal,
                        edit: None,
                    });
                }
            }
            sidebar.set_rows(sec_idx, rows);
        }

        // Default-select the first issue in the first non-empty repo section.
        if sidebar.active_section().is_none() {
            'find_default: for sec_idx in 0..repos.len() {
                let groups = self.pipeline_groups_for_repo(&repos[sec_idx]);
                if !groups.is_empty() {
                    sidebar.set_active_section(Some(sec_idx));
                    // Select the first issue row (path [0, 0] = first lifecycle
                    // group header expanded → first issue).
                    sidebar.set_selected_path(sec_idx, Some(vec![0u16, 0u16]));
                    break 'find_default;
                }
            }
        }

        self.pipeline_repo_names = repos;
        self.pipeline_sidebar = sidebar;

        // Restore previous selection if the issue still exists.
        if let Some((repo, num)) = prev_sel {
            'outer: for (sec_idx, repo_key) in self.pipeline_repo_names.iter().enumerate() {
                let groups = self.pipeline_groups_for_repo(repo_key);
                for (li, (_, issue_idxs)) in groups.iter().enumerate() {
                    for (ii, &idx) in issue_idxs.iter().enumerate() {
                        let issue = &self.pipeline_issues[idx];
                        if issue.repo_slug == repo && issue.number == num {
                            self.pipeline_sel = Some(idx);
                            self.pipeline_sidebar.set_active_section(Some(sec_idx));
                            self.pipeline_sidebar
                                .set_selected_path(sec_idx, Some(vec![li as u16, ii as u16]));
                            break 'outer;
                        }
                    }
                }
            }
        }
        // Sync `pipeline_sel` to the sidebar's actual selection.
        self.pipeline_sel = self.selected_pipeline_index();
        // Restore per-section collapse state by repo name.  Sections that
        // didn't exist before stay expanded by default (a new repo
        // appearing shouldn't be hidden from the user).
        for (i, name) in self.pipeline_repo_names.iter().enumerate() {
            if let Some(&was_collapsed) = prev_collapsed.get(name) {
                self.pipeline_sidebar.set_collapsed(i, was_collapsed);
            }
        }
        // Restore panel scroll so the visible area doesn't jump back to the
        // top on every refresh.  Done after selection restore so the active-
        // section's keyboard nav state is set up correctly before scroll is
        // applied (scroll_to_active_section calls in handle_key_selection
        // assume an active section).
        self.pipeline_sidebar.set_panel_scroll(prev_panel_scroll);
    }

    /// Resolve the SidebarSystem's current selection to a `pipeline_issues`
    /// index.  Paths are two-level: `[lifecycle_group_idx, issue_idx_in_group]`.
    /// A one-level path (group header selected) returns `None`.
    fn selected_pipeline_index(&self) -> Option<usize> {
        let section = self.pipeline_sidebar.active_section()?;
        let path = self.pipeline_sidebar.selected_path(section)?;
        if path.len() < 2 {
            return None; // group header selected, not an issue
        }
        let li = path[0] as usize;
        let ii = path[1] as usize;
        let repo_key = self.pipeline_repo_names.get(section)?;
        let groups = self.pipeline_groups_for_repo(repo_key);
        let (_, issue_idxs) = groups.get(li)?;
        issue_idxs.get(ii).copied()
    }

    /// Resolve the per-stage status of an issue from existing assignments.
    ///
    /// "work" is the first stage and matches assignments with
    /// `assignment_type` `None` or `"work"`.  Other stage names match
    /// assignments by exact `assignment_type`.  The "merge" stage is
    /// special-cased to read from the `merge_queue` table instead, since
    /// merges are not modelled as assignments.
    fn stage_status_for(
        &self,
        issue: &PipelineIssue,
        stage: &str,
    ) -> StageStatus {
        if stage == "merge" {
            return self.merge_stage_status_for(issue);
        }
        if stage == "test" {
            return self.test_stage_status_for(issue);
        }
        let matching = self.assignments_for_stage(issue, stage);
        if matching.iter().any(|a| a.status == "running") {
            return StageStatus::Active;
        }
        let latest = matching.iter().max_by(|a, b| {
            a.dispatched_at
                .partial_cmp(&b.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        if let Some(latest) = latest {
            let verdict = match latest.status.as_str() {
                "done" => Some(StageStatus::Done),
                "failed" => Some(StageStatus::Failed),
                _ => None,
            };
            if let Some(v) = verdict {
                // #193: a Done/Failed verdict is only trustworthy if no upstream
                // stage has been re-dispatched since. If any upstream's latest
                // dispatched_at is newer than this stage's latest, the verdict
                // is against an older revision — render as Stale.
                if let Some(this_dispatched) = latest.dispatched_at {
                    if self
                        .upstream_max_dispatched_at(issue, stage)
                        .map_or(false, |u| u > this_dispatched)
                    {
                        return StageStatus::Stale;
                    }
                }
                return v;
            }
        }
        // No matching assignment found.  For closed issues this means the stage
        // was never run through coord — use Skipped so the UI can distinguish
        // "waiting to run" (Pending) from "never ran, issue already closed"
        // (Skipped).
        if issue.is_closed {
            StageStatus::Skipped
        } else {
            StageStatus::Pending
        }
    }

    /// Return all assignments for `issue` whose `assignment_type` matches
    /// `stage`. When pipeline has no Plan gate, plan-typed assignments also
    /// count as Work so the stage advances correctly.
    fn assignments_for_stage<'a>(
        &'a self,
        issue: &PipelineIssue,
        stage: &str,
    ) -> Vec<&'a Assignment> {
        self.data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| {
                if let Some(local) = &issue.coord_repo {
                    a.repo == *local
                } else {
                    true
                }
            })
            .filter(|a| {
                let t = a.assignment_type.as_deref().unwrap_or("work");
                if stage == "work"
                    && !self.data.pipeline_require_plan
                    && !self.issue_has_plan_assignment(issue)
                {
                    // Legacy fold: when there's no Plan stage in this
                    // issue's strip, plan-typed assignments are still
                    // counted as Work so a `--plan-only` dispatch
                    // without `require_plan` doesn't disappear.
                    t == "work" || t == "plan"
                } else {
                    t == stage
                }
            })
            .collect()
    }

    /// Return the max `dispatched_at` across all stages strictly upstream of
    /// `stage`, or None if no upstream stage has an assignment.
    ///
    /// Used by `stage_status_for` to detect stale downstream verdicts.
    fn upstream_max_dispatched_at(
        &self,
        issue: &PipelineIssue,
        stage: &str,
    ) -> Option<f64> {
        let names = self.pipeline_stage_names_for_issue(issue);
        let idx = names.iter().position(|s| s == stage)?;
        if idx == 0 {
            return None;
        }
        names[..idx]
            .iter()
            .flat_map(|s| self.assignments_for_stage(issue, s))
            .filter_map(|a| a.dispatched_at)
            .fold(None, |acc, t| {
                Some(acc.map_or(t, |x: f64| x.max(t)))
            })
    }

    /// Resolve the merge stage status from `merge_queue` entries.
    ///
    /// `merged` → Done, `open`/`queued` → Active, `failed` → Failed, anything
    /// else (or no entry) → Pending for open issues, Skipped for closed issues
    /// (the merge never ran through coord).
    fn merge_stage_status_for(&self, issue: &PipelineIssue) -> StageStatus {
        // #241: a running conflict-fix worker keeps the Merge stage Active —
        // the auto-rebase is part of the merge phase, not a separate stage.
        if self.has_active_conflict_fix(issue) {
            return StageStatus::Active;
        }
        let entry = self
            .data
            .merge_queue
            .iter()
            .find(|m| m.issue_number == Some(issue.number));
        match entry.map(|e| e.state.as_str()) {
            Some("merged") => StageStatus::Done,
            Some("open") | Some("queued") => StageStatus::Active,
            // #241: HUMAN_REQUIRED (failed conflict-fix) — the merge needs a
            // human, so Failed.  `failed` (legacy / direct) is also Failed.
            Some("failed") | Some("human_required") => StageStatus::Failed,
            _ => if issue.is_closed { StageStatus::Skipped } else { StageStatus::Pending },
        }
    }

    /// #241: is there a conflict-fix worker currently in flight for *issue*?
    fn has_active_conflict_fix(&self, issue: &PipelineIssue) -> bool {
        self.data.assignments.iter().any(|a| {
            a.issue_number == issue.number
                && a.assignment_type.as_deref() == Some("conflict-fix")
                && (a.status == "running" || a.status == "pending")
        })
    }

    /// #200: Resolve the Test gate status from `test_state` on the latest
    /// Work assignment for `issue`.
    ///
    /// `passed`/`skipped` → Done; `failed` → Failed; otherwise Pending while
    /// Work is settled and Pending/Skipped while Work isn't done yet.
    /// #235: When a Phase 1 build is in flight for the latest Work
    /// assignment, the badge goes Active ("Building") even though Test is
    /// still a human gate — the user needs to know something is running.
    fn test_stage_status_for(&self, issue: &PipelineIssue) -> StageStatus {
        // Test is gated on Work; if Work hasn't finished, Test isn't actionable.
        let work_status = self.stage_status_for_internal_work(issue);
        if work_status != StageStatus::Done {
            // Work is still Active/Pending/Failed/Stale — Test inherits Pending
            // (or Skipped for closed issues that bypassed Work).
            return if issue.is_closed { StageStatus::Skipped } else { StageStatus::Pending };
        }
        // Work is done — read the verdict off the latest Work assignment.
        let work = self.assignments_for_stage(issue, "work");
        let latest = work.iter().max_by(|a, b| {
            a.dispatched_at
                .partial_cmp(&b.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        // #235: Phase 1 build in flight beats any prior verdict — the user
        // pressed `B` to re-test, so the old verdict is no longer current.
        if let Some(a) = latest.as_ref() {
            if self.test_build_in_flight(&a.id) {
                return StageStatus::Active;
            }
        }
        match latest.and_then(|a| a.test_state.as_deref()) {
            Some("passed") | Some("skipped") => StageStatus::Done,
            Some("failed") => StageStatus::Failed,
            _ => StageStatus::Pending,
        }
    }

    /// Compute the Work stage status without going through the dispatch in
    /// `stage_status_for` (which would special-case "test" too). Used by
    /// `test_stage_status_for` to decide whether Test is actionable yet.
    fn stage_status_for_internal_work(&self, issue: &PipelineIssue) -> StageStatus {
        let matching = self.assignments_for_stage(issue, "work");
        if matching.iter().any(|a| a.status == "running") {
            return StageStatus::Active;
        }
        let latest = matching.iter().max_by(|a, b| {
            a.dispatched_at
                .partial_cmp(&b.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        if let Some(latest) = latest {
            match latest.status.as_str() {
                "done" => return StageStatus::Done,
                "failed" => return StageStatus::Failed,
                _ => {}
            }
        }
        if issue.is_closed { StageStatus::Skipped } else { StageStatus::Pending }
    }

    /// Returns the *display* current stage for the sidebar badge — the
    /// first non-Done/non-Skipped stage, or "done" once every meaningful
    /// stage is Done or Skipped.
    ///
    /// Skipped stages (closed-issue stages that never ran through coord) are
    /// treated the same as Done for badge purposes: they don't represent a
    /// meaningful "current" action and should not halt the badge at "work".
    fn derive_current_stage(&self, issue: &PipelineIssue) -> String {
        let stages = self.pipeline_stage_names();
        for s in &stages {
            let st = self.stage_status_for(issue, s);
            if st != StageStatus::Done && st != StageStatus::Skipped {
                return s.clone();
            }
        }
        "done".to_string()
    }

    /// Build the quadraui `PipelineView` widget for the selected issue.
    ///
    /// The `[Go]` button is attached to the leftmost stage that is
    /// (a) Pending, (b) dispatchable from the TUI (work, review, merge),
    /// (c) preceded only by Done stages, and (d) for an issue we can map
    /// back to a coordinator-local repo.
    ///
    /// Returns `None` for closed issues that have zero assignment rows in the
    /// DB — these were resolved outside the coord pipeline, so showing an
    /// all-Skipped stage widget would be misleading. The caller will render
    /// the "closed without coord pipeline" placeholder instead.
    /// Move the Pipeline > Stages focus to the next stage (right).
    /// Wraps to the first stage from the last.  Resets the content
    /// scroll so the user starts at the top of the new content.
    fn focus_next_pipeline_stage(&mut self) {
        let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i))
        else { return; };
        let stages = self.pipeline_stage_names_for_issue(issue);
        if stages.is_empty() {
            return;
        }
        let next = match self.pipeline_focused_stage {
            None => 0,
            Some(i) => (i + 1) % stages.len(),
        };
        self.pipeline_focused_stage = Some(next);
        self.pipeline_stage_content_scroll = 0;
    }

    /// Move the Pipeline > Stages focus to the previous stage (left).
    /// Wraps to the last stage from the first.
    fn focus_prev_pipeline_stage(&mut self) {
        let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i))
        else { return; };
        let stages = self.pipeline_stage_names_for_issue(issue);
        if stages.is_empty() {
            return;
        }
        let prev = match self.pipeline_focused_stage {
            None => stages.len() - 1,
            Some(i) => (i + stages.len() - 1) % stages.len(),
        };
        self.pipeline_focused_stage = Some(prev);
        self.pipeline_stage_content_scroll = 0;
    }

    fn build_pipeline_widget(&self) -> Option<QuiPipelineView> {
        let idx = self.pipeline_sel?;
        let issue = self.pipeline_issues.get(idx)?;

        // Closed with no assignment rows → suppress widget; placeholder message shown.
        if issue.is_closed && !self.issue_has_any_assignment(issue) {
            return None;
        }

        // Use the per-issue stage list so a plan-typed assignment
        // shows up as a Plan stage even when `pipeline_require_plan`
        // is false globally (the #262 right-click → Start with Plan path).
        let stage_names = self.pipeline_stage_names_for_issue(issue);

        // Precompute every stage's status once so we can check predecessors
        // without re-querying.
        let statuses: Vec<StageStatus> = stage_names
            .iter()
            .map(|name| self.stage_status_for(issue, name))
            .collect();

        let mut go_attached = false;
        let stages: Vec<QuiPipelineStage> = stage_names
            .iter()
            .enumerate()
            .map(|(i, name)| {
                let status = statuses[i].clone();
                let mut label = match name.as_str() {
                    "work" => "Work".to_string(),
                    other => {
                        let mut s = other.to_string();
                        if let Some(c) = s.get_mut(0..1) {
                            c.make_ascii_uppercase();
                        }
                        s
                    }
                };
                // #235: When Phase 1 is mid-build for this issue, swap the
                // Test label to "Building" so the Active badge has meaningful
                // text (otherwise it'd just say "Test" while running).
                if name == "test" && status == StageStatus::Active {
                    if let Some(work_id) = self.assignments_for_stage(issue, "work")
                        .iter()
                        .max_by(|a, b| a.dispatched_at.partial_cmp(&b.dispatched_at)
                            .unwrap_or(std::cmp::Ordering::Equal))
                        .map(|a| a.id.clone())
                    {
                        if self.test_build_in_flight(&work_id) {
                            label = "Building".to_string();
                        }
                    }
                }
                // Skipped counts as "settled" for prior_all_done: a closed-issue
                // stage that never ran is logically done.
                let prior_all_done = statuses[..i].iter().all(|s| {
                    *s == StageStatus::Done || *s == StageStatus::Skipped
                });
                let action = if !go_attached
                    && status == StageStatus::Pending
                    && prior_all_done
                    && is_dispatchable_stage(name)
                    && issue.coord_repo.is_some()
                {
                    go_attached = true;
                    Some("Go".to_string())
                } else if (status == StageStatus::Failed || status == StageStatus::Stale)
                    && prior_all_done
                    && is_dispatchable_stage(name)
                    && issue.coord_repo.is_some()
                {
                    // Failed → user-initiated retry of the failed dispatch.
                    // Stale → re-dispatch because an upstream stage was re-run.
                    // In both cases the user has to act; only show Retry when
                    // predecessors are settled so we don't race a running upstream.
                    Some("Retry".to_string())
                } else {
                    None
                };
                QuiPipelineStage {
                    label,
                    status,
                    action,
                }
            })
            .collect();

        // Clamp the persisted focus to the current stage count so a
        // smaller pipeline (e.g. issue without a Plan stage) doesn't
        // get a focus pointing past its last stage.
        let focused_stage = self
            .pipeline_focused_stage
            .filter(|&i| i < stages.len());
        Some(QuiPipelineView {
            id: WidgetId::new("pipeline:detail"),
            stages,
            focused_stage,
        })
    }

    /// Pick the best machine to dispatch `coord_repo` work to.
    ///
    /// Prefers reachable machines that list `coord_repo` in their `repos`
    /// and have the fewest currently-running assignments.  Returns `None`
    /// when no reachable machine claims this repo.
    fn best_machine_for(&self, coord_repo: &str) -> Option<&Machine> {
        self.data
            .machines
            .iter()
            .filter(|m| m.reachable && m.repos.iter().any(|r| r == coord_repo))
            .min_by_key(|m| m.active_count)
    }

    /// #200: Find the latest Work assignment id for the currently-selected
    /// pipeline issue. Returns None if no issue is selected or no work assignment
    /// exists yet.
    fn pipeline_selected_work_id(&self) -> Option<String> {
        let issue = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i))?;
        let work = self.assignments_for_stage(issue, "work");
        let latest = work.iter().max_by(|a, b| {
            a.dispatched_at
                .partial_cmp(&b.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        })?;
        Some(latest.id.clone())
    }

    /// #200: Apply a Test gate verdict to the selected issue's latest Work
    /// assignment. Toasts the outcome. Returns true on success (a redraw is
    /// needed regardless).
    fn record_test_verdict(&mut self, verdict: &str, reason: Option<&str>) -> bool {
        let Some(work_id) = self.pipeline_selected_work_id() else {
            self.push_toast(
                "Test verdict skipped",
                "No work assignment to mark — dispatch Work first.",
                ToastSeverity::Error,
            );
            return false;
        };
        match record_test_verdict_db(&work_id, verdict, reason) {
            Ok(()) => {
                let verb = match verdict {
                    "passed" => "PASSED",
                    "failed" => "FAILED",
                    "skipped" => "SKIPPED",
                    _ => verdict,
                };
                // #236: include the next-action hint so the user doesn't
                // have to read the status bar to find what to press.
                let suffix = match verdict {
                    "failed" => " — press R to re-dispatch Work",
                    "passed" | "skipped" => " — press R to dispatch review",
                    _ => "",
                };
                self.push_toast(
                    "Test gate",
                    &format!(
                        "Marked {} (work {}){}",
                        verb,
                        &work_id[..8.min(work_id.len())],
                        suffix,
                    ),
                    ToastSeverity::Info,
                );
                self.refresh();
                true
            }
            Err(e) => {
                self.push_toast(
                    "Test verdict failed",
                    &format!("{}", e),
                    ToastSeverity::Error,
                );
                false
            }
        }
    }

    /// #200: True when the selected issue's Test stage is pending and ready
    /// for a verdict (Work is Done, no verdict yet).
    fn test_gate_actionable(&self) -> bool {
        let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i))
        else { return false; };
        let stages = self.pipeline_stage_names();
        if !stages.iter().any(|s| s == "test") {
            return false;
        }
        self.test_stage_status_for(issue) == StageStatus::Pending
            && self.stage_status_for_internal_work(issue) == StageStatus::Done
    }

    /// #236: True when the Test gate just passed (or was skipped) and the
    /// Review stage is Pending — the user can press `R` to dispatch the
    /// review immediately instead of waiting for the reconcile auto-dispatch.
    ///
    /// Drives the status-bar hint swap so the affordance is discoverable.
    /// The R keybind's existing `dispatch_pipeline_active_go` path already
    /// targets the Pending Review stage in this state; this predicate just
    /// surfaces it.
    fn can_dispatch_review_after_test_done(&self) -> bool {
        if self.active_view != SidebarView::Pipeline {
            return false;
        }
        let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i))
        else { return false; };
        let stages = self.pipeline_stage_names();
        if !stages.iter().any(|s| s == "review") {
            return false;
        }
        // Test must be Done (passed/skipped) — Skipped (closed-issue path)
        // is not a "fresh pass" the user is acting on, so exclude it.
        let test_done = stages.iter().any(|s| s == "test")
            && self.test_stage_status_for(issue) == StageStatus::Done;
        if !test_done {
            return false;
        }
        // Review must be Pending and dispatchable (we have a coord_repo).
        let review_pending = self.stage_status_for(issue, "review") == StageStatus::Pending;
        review_pending && issue.coord_repo.is_some()
    }

    /// #236: True when the Test gate just failed and the user needs to
    /// bounce back to Work — fix the code, re-dispatch.
    ///
    /// The Pipeline widget's button-attachment logic does NOT attach a
    /// `[Retry]` to a Failed Test stage (test isn't dispatchable via the
    /// worker pipeline — it's a human gate), and Work shows as Done
    /// (the prior Work succeeded against the agent's own check; Test is
    /// what's failed).  So without this, the user has no in-TUI keybind
    /// to "send Work back for another iteration based on the test
    /// failure feedback".  When this returns true, the `R` keybind
    /// short-circuits and calls `dispatch_pipeline_work()` for a fresh
    /// Work attempt.
    fn can_bounce_work_after_test_fail(&self) -> bool {
        if self.active_view != SidebarView::Pipeline {
            return false;
        }
        let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i))
        else { return false; };
        let stages = self.pipeline_stage_names();
        if !stages.iter().any(|s| s == "test") {
            return false;
        }
        // Test must be Failed and the failure must apply to a Done Work
        // (otherwise the user's next step isn't "re-dispatch Work").
        let test_failed = self.test_stage_status_for(issue) == StageStatus::Failed;
        let work_done = self.stage_status_for_internal_work(issue) == StageStatus::Done;
        test_failed && work_done && issue.coord_repo.is_some()
    }

    /// #235: True when a Phase 1 build for the given work assignment is
    /// currently running (entry present in `test_build_jobs`).  Removed when
    /// `poll_test_build_jobs` drains the completion message.
    fn test_build_in_flight(&self, work_id: &str) -> bool {
        self.test_build_jobs.contains_key(work_id)
    }

    /// #235: Gate for the `B` keybind.  True when:
    /// - the Pipeline view is active,
    /// - the Test gate is actionable (Work Done, no verdict yet),
    /// - the latest Work assignment has a branch recorded, and
    /// - no Phase 1 build is already in flight for this work id.
    ///
    /// We do NOT check `repo.build_command` here: `coord test` handles a
    /// missing build_command by just doing the checkout, which is still
    /// useful (the user gets the branch locally for manual inspection).
    fn can_trigger_test_build(&self) -> bool {
        if self.active_view != SidebarView::Pipeline {
            return false;
        }
        if !self.test_gate_actionable() {
            return false;
        }
        let Some(work_id) = self.pipeline_selected_work_id() else {
            return false;
        };
        if self.test_build_in_flight(&work_id) {
            return false;
        }
        let work = self.data.assignments.iter().find(|a| a.id == work_id);
        work.and_then(|a| a.branch.as_ref()).is_some()
    }

    /// #235 Phase 1 trigger: spawn `coord test <work_id>` on the local
    /// machine in a background thread, capturing combined stdout+stderr to
    /// `~/.coord/test-build-<work_id>.log`.  Returns `true` when a job was
    /// scheduled (a redraw is warranted for the new "Building" badge).
    ///
    /// Idempotent: a no-op when a build for `work_id` is already in flight.
    fn spawn_test_build(&mut self, work_id: String, branch: String, issue_number: u64) -> bool {
        if self.test_build_jobs.contains_key(&work_id) {
            return false;
        }
        let log_dir = coord_dir();
        if let Err(e) = std::fs::create_dir_all(&log_dir) {
            self.push_toast(
                "Build setup failed",
                &format!("create {} failed: {}", log_dir.display(), e),
                ToastSeverity::Error,
            );
            return false;
        }
        let log_path = log_dir.join(format!("test-build-{}.log", work_id));
        let cfg_path = self.command_runner.config_path.clone();

        let (tx, rx) = std::sync::mpsc::channel::<TestBuildOutcome>();
        let work_id_thread = work_id.clone();
        let log_path_thread = log_path.clone();
        std::thread::spawn(move || {
            use std::process::{Command, Stdio};
            let mut cmd = Command::new("coord");
            cmd.arg("test");
            if let Some(cfg) = &cfg_path {
                cmd.arg("--config").arg(cfg);
            }
            cmd.arg(&work_id_thread);
            // Belt-and-braces: even though main.rs sets GIT_TERMINAL_PROMPT=0
            // and BatchMode=yes, explicitly null-out stdin so no descendant
            // can grab the TUI's TTY for a prompt.  Combined with main.rs's
            // env vars this guarantees the build either succeeds against
            // ssh-agent-loaded keys or fails fast with a clear error.
            cmd.stdin(Stdio::null());
            let (exit_code, first_error) = match cmd.output() {
                Ok(out) => {
                    let mut buf = Vec::with_capacity(out.stdout.len() + out.stderr.len() + 64);
                    buf.extend_from_slice(b"--- stdout ---\n");
                    buf.extend_from_slice(&out.stdout);
                    buf.extend_from_slice(b"\n--- stderr ---\n");
                    buf.extend_from_slice(&out.stderr);
                    let _ = std::fs::write(&log_path_thread, buf);
                    let code = out.status.code().unwrap_or(-1);
                    // On failure, grab the first non-empty stderr line (or
                    // stdout if stderr is empty) for the toast.  Strip the
                    // "error: " prefix that `coord` prepends so the user
                    // sees the actual diagnostic.
                    let first = if code != 0 {
                        let pick = |bytes: &[u8]| -> Option<String> {
                            String::from_utf8_lossy(bytes)
                                .lines()
                                .map(|l| l.trim())
                                .find(|l| !l.is_empty())
                                .map(|l| l.trim_start_matches("error: ").to_string())
                        };
                        pick(&out.stderr)
                            .or_else(|| pick(&out.stdout))
                            .unwrap_or_default()
                    } else {
                        String::new()
                    };
                    (code, first)
                }
                Err(e) => {
                    let msg = format!("failed to spawn coord test: {}", e);
                    let _ = std::fs::write(&log_path_thread, format!("{}\n", msg));
                    (-1, msg)
                }
            };
            let _ = tx.send(TestBuildOutcome { exit_code, first_error });
        });

        self.push_toast(
            "Phase 1 build started",
            &format!(
                "#{} on {} — fetching and building…",
                issue_number, branch
            ),
            ToastSeverity::Info,
        );
        self.test_build_jobs.insert(
            work_id.clone(),
            TestBuildJob {
                work_id,
                issue_number,
                branch,
                log_path,
                started_at: Instant::now(),
                rx,
            },
        );
        true
    }

    /// #235: Drain completed Phase 1 build jobs, toast their outcome, and
    /// remove them from the in-flight map.  Returns `true` when at least
    /// one job finished (a redraw is needed so the "Building" badge clears).
    fn poll_test_build_jobs(&mut self) -> bool {
        if self.test_build_jobs.is_empty() {
            return false;
        }
        use std::sync::mpsc::TryRecvError;
        let mut done: Vec<(String, Result<TestBuildOutcome, ()>)> = Vec::new();
        for (id, job) in self.test_build_jobs.iter() {
            match job.rx.try_recv() {
                Ok(outcome) => done.push((id.clone(), Ok(outcome))),
                Err(TryRecvError::Disconnected) => done.push((id.clone(), Err(()))),
                Err(TryRecvError::Empty) => {}
            }
        }
        if done.is_empty() {
            return false;
        }
        for (id, result) in done {
            let job = self.test_build_jobs.remove(&id).expect("just observed");
            let dur_secs = job.started_at.elapsed().as_secs();
            // #271 part 2: persist the outcome so the Pipeline detail
            // panel can show "Last build: …" long after the toast
            // expires.  Pull values out before move-into-toast paths.
            let persist_exit;
            let persist_first_error;
            match &result {
                Ok(o) => {
                    persist_exit = o.exit_code;
                    persist_first_error = o.first_error.clone();
                }
                Err(()) => {
                    persist_exit = -1;
                    persist_first_error = "build worker disappeared".to_string();
                }
            }
            self.last_test_builds.insert(
                id.clone(),
                TestBuildResult {
                    branch: job.branch.clone(),
                    issue_number: job.issue_number,
                    exit_code: persist_exit,
                    first_error: persist_first_error,
                    log_path: job.log_path.clone(),
                    duration_secs: dur_secs,
                    finished_at: Instant::now(),
                },
            );
            match result {
                Ok(TestBuildOutcome { exit_code: 0, .. }) => {
                    self.push_toast(
                        "Phase 1 build ✓",
                        &format!(
                            "#{} ready to test on {} ({}s) — press P / F / S",
                            job.issue_number, job.branch, dur_secs
                        ),
                        ToastSeverity::Info,
                    );
                }
                Ok(TestBuildOutcome { exit_code: code, first_error }) => {
                    // Truncate the error to ~120 chars so the toast stays
                    // readable; the full log is at log_path for details.
                    let snippet = if first_error.is_empty() {
                        format!("see {}", job.log_path.display())
                    } else {
                        let trimmed: String = first_error.chars().take(120).collect();
                        if first_error.chars().count() > 120 {
                            format!("{}… (see {})", trimmed, job.log_path.display())
                        } else {
                            format!("{} (see {})", trimmed, job.log_path.display())
                        }
                    };
                    self.push_toast(
                        "Phase 1 build ✗",
                        &format!("#{} exit {}: {}", job.issue_number, code, snippet),
                        ToastSeverity::Error,
                    );
                }
                Err(()) => {
                    self.push_toast(
                        "Phase 1 build ✗",
                        &format!(
                            "#{} build worker disappeared — see {}",
                            job.issue_number, job.log_path.display()
                        ),
                        ToastSeverity::Error,
                    );
                }
            }
        }
        true
    }

    /// Dispatch the action button (`[Go]` or `[Retry]`) attached to a
    /// specific stage index. Branches on the stage's current status: a
    /// Failed stage gets retry semantics; everything else gets fresh
    /// dispatch.  Falls back to a status message for stages we don't
    /// know how to dispatch.
    fn dispatch_pipeline_stage(&mut self, stage_idx: usize) -> bool {
        // Per-issue stages must match what `build_pipeline_widget`
        // rendered — otherwise a click on the Plan stage (visible only
        // when the issue has a plan assignment) would resolve to the
        // wrong stage_name.
        let Some(sel) = self.pipeline_sel else { return false; };
        let Some(issue) = self.pipeline_issues.get(sel).cloned() else { return false; };
        let stage_name = match self
            .pipeline_stage_names_for_issue(&issue)
            .get(stage_idx)
            .cloned()
        {
            Some(s) => s,
            None => return false,
        };
        // Failed → retry the failed assignment (re-dispatch via `coord retry`).
        // Stale → fall through to fresh dispatch: the previous attempt SUCCEEDED
        //         against an older revision, so there is no failed-row to retry;
        //         we want a brand-new assignment against the new upstream.
        //         (Routing Stale into the retry path produces a misleading
        //         "no failed assignment found" error.)
        let stage_status = self.stage_status_for(&issue, &stage_name);
        let is_retry = stage_status == StageStatus::Failed;
        match stage_name.as_str() {
            "plan" => {
                if is_retry {
                    self.retry_pipeline_assignment(&issue, "plan")
                } else {
                    self.dispatch_pipeline_plan()
                }
            }
            "work" => {
                if is_retry {
                    self.retry_pipeline_assignment(&issue, "work")
                } else {
                    self.dispatch_pipeline_work()
                }
            }
            "review" => {
                if is_retry {
                    self.retry_pipeline_assignment(&issue, "review")
                } else {
                    self.dispatch_pipeline_review()
                }
            }
            // Merge has no assignment row to retry against — re-running
            // `coord merge` is the right call for both fresh and retry.
            "merge" => self.dispatch_pipeline_merge(),
            other => {
                self.pipeline_status = Some((
                    format!("stage '{}' not dispatchable from TUI", other),
                    Instant::now(),
                ));
                false
            }
        }
    }

    /// Dispatch the action button on whichever stage currently owns it.
    /// Used by the Enter key handler where we don't have a stage index
    /// from a click — falls through to the first stage with an attached
    /// `[Go]` or `[Retry]`.
    fn dispatch_pipeline_active_go(&mut self) -> bool {
        let widget = match self.build_pipeline_widget() {
            Some(w) => w,
            None => return false,
        };
        let stage_idx = widget.stages.iter().position(|s| s.action.is_some());
        match stage_idx {
            Some(i) => self.dispatch_pipeline_stage(i),
            None => false,
        }
    }

    /// Retry the latest failed `<stage>` assignment for `issue` via
    /// `coord retry <assignment_id>`.  No-op with a status message when
    /// no matching failed assignment is found.
    fn retry_pipeline_assignment(&mut self, issue: &PipelineIssue, stage: &str) -> bool {
        let Some(coord_repo) = issue.coord_repo.clone() else {
            self.pipeline_status = Some((
                format!(
                    "no local repo mapping for {} — add it to coordinator.yml",
                    issue.repo_slug
                ),
                Instant::now(),
            ));
            return false;
        };
        let assignment_id = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number && a.repo == coord_repo)
            .filter(|a| {
                let t = a.assignment_type.as_deref().unwrap_or("work");
                if stage == "work" && !self.issue_has_plan_assignment(issue) {
                    // Same legacy fold as `assignments_for_stage` — only
                    // applies when the per-issue strip has no Plan stage.
                    t == "work" || t == "plan"
                } else {
                    t == stage
                }
            })
            .find(|a| a.status == "failed")
            .map(|a| a.id.clone());
        let Some(id) = assignment_id else {
            self.pipeline_status = Some((
                format!(
                    "no failed {} assignment found for #{}",
                    stage, issue.number
                ),
                Instant::now(),
            ));
            return false;
        };
        let spawned = self.command_runner.spawn(&["retry", &id]);
        if spawned {
            self.pipeline_status = Some((
                format!("retry dispatched for {} #{}", stage, issue.number),
                Instant::now(),
            ));
        } else {
            self.pipeline_status = Some((
                "another command is running — try again in a moment".to_string(),
                Instant::now(),
            ));
        }
        spawned
    }

    /// Dispatch the Work stage.
    ///
    /// If a completed Plan assignment exists for this issue, runs
    /// `coord approve-plan <plan_id>` — which uses the plan output as the
    /// briefing for the new work assignment.  Otherwise falls back to a
    /// fresh `coord assign <machine> <repo> <issue>`.
    fn dispatch_pipeline_work(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else { return false; };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else { return false; };
        let Some(coord_repo) = issue.coord_repo.clone() else {
            self.pipeline_status = Some((
                format!(
                    "no local repo mapping for {} — add it to coordinator.yml",
                    issue.repo_slug
                ),
                Instant::now(),
            ));
            return false;
        };

        // If a done plan exists for this issue, approve it — that path
        // dispatches the work assignment with the plan output baked into
        // the briefing.
        if let Some(plan_id) = self.find_done_plan_assignment_id(&issue, &coord_repo) {
            let spawned = self.command_runner.spawn(&["approve-plan", &plan_id]);
            if spawned {
                self.pipeline_status = Some((
                    format!(
                        "approving plan {} → dispatching work for #{}",
                        &plan_id[..plan_id.len().min(8)],
                        issue.number
                    ),
                    Instant::now(),
                ));
            } else {
                self.pipeline_status = Some((
                    "another command is running — try again in a moment".to_string(),
                    Instant::now(),
                ));
            }
            return spawned;
        }

        let Some(machine) = self.best_machine_for(&coord_repo) else {
            self.pipeline_status = Some((
                format!(
                    "no reachable machine for {} — queued (issue #{})",
                    coord_repo, issue.number
                ),
                Instant::now(),
            ));
            return false;
        };
        let machine_name = machine.name.clone();
        let issue_str = issue.number.to_string();
        // Inject session-level model override when the user configured one for
        // this machine in Settings → Dispatch → Per-Machine Model Overrides.
        let model_str = self
            .settings
            .machine_model
            .get(&machine_name)
            .map(|p| p.as_str().to_string());
        let mut cmd: Vec<String> = vec![
            "assign".into(),
            machine_name.clone(),
            coord_repo.clone(),
            issue_str.clone(),
        ];
        if let Some(ref m) = model_str {
            cmd.push("--model".into());
            cmd.push(m.clone());
        }
        let cmd_refs: Vec<&str> = cmd.iter().map(|s| s.as_str()).collect();
        let spawned = self.command_runner.spawn(&cmd_refs);
        if spawned {
            self.pipeline_status = Some((
                format!("dispatched #{} → {}", issue.number, machine_name),
                Instant::now(),
            ));
        } else {
            self.pipeline_status = Some((
                "another command is running — try again in a moment".to_string(),
                Instant::now(),
            ));
        }
        spawned
    }

    /// Dispatch the Plan stage: `coord assign --plan-only <machine> <repo> <issue>`.
    fn dispatch_pipeline_plan(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else { return false; };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else { return false; };
        let Some(coord_repo) = issue.coord_repo.clone() else {
            self.pipeline_status = Some((
                format!(
                    "no local repo mapping for {} — add it to coordinator.yml",
                    issue.repo_slug
                ),
                Instant::now(),
            ));
            return false;
        };
        let Some(machine) = self.best_machine_for(&coord_repo) else {
            self.pipeline_status = Some((
                format!(
                    "no reachable machine for {} — queued (issue #{})",
                    coord_repo, issue.number
                ),
                Instant::now(),
            ));
            return false;
        };
        let machine_name = machine.name.clone();
        let issue_str = issue.number.to_string();
        // Inject session-level model override when configured for this machine.
        let model_str = self
            .settings
            .machine_model
            .get(&machine_name)
            .map(|p| p.as_str().to_string());
        let mut cmd: Vec<String> = vec![
            "assign".into(),
            "--plan-only".into(),
            machine_name.clone(),
            coord_repo.clone(),
            issue_str.clone(),
        ];
        if let Some(ref m) = model_str {
            cmd.push("--model".into());
            cmd.push(m.clone());
        }
        let cmd_refs: Vec<&str> = cmd.iter().map(|s| s.as_str()).collect();
        let spawned = self.command_runner.spawn(&cmd_refs);
        if spawned {
            self.pipeline_status = Some((
                format!("plan dispatched for #{} → {}", issue.number, machine_name),
                Instant::now(),
            ));
        } else {
            self.pipeline_status = Some((
                "another command is running — try again in a moment".to_string(),
                Instant::now(),
            ));
        }
        spawned
    }

    /// Find the most recent done plan-typed assignment for this issue.
    /// Used by Work [Go] to decide between `approve-plan` and a fresh
    /// `coord assign`.
    fn find_done_plan_assignment_id(
        &self,
        issue: &PipelineIssue,
        coord_repo: &str,
    ) -> Option<String> {
        self.data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number && a.repo == coord_repo)
            .filter(|a| a.assignment_type.as_deref() == Some("plan"))
            .find(|a| a.status == "done")
            .map(|a| a.id.clone())
    }

    /// Dispatch the Review stage: `coord notify`.
    ///
    /// `coord notify` polls every machine for completion and auto-dispatches
    /// a review when a worker has finished — this is a session-wide poll,
    /// not scoped to a single issue, but in practice it's the right call:
    /// the worker we want a review for has just finished, and notify is
    /// idempotent for already-reviewed work.
    fn dispatch_pipeline_review(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else { return false; };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else { return false; };
        let spawned = self.command_runner.spawn(&["notify"]);
        if spawned {
            self.pipeline_status = Some((
                format!("notify dispatched for #{}", issue.number),
                Instant::now(),
            ));
        } else {
            self.pipeline_status = Some((
                "another command is running — try again in a moment".to_string(),
                Instant::now(),
            ));
        }
        spawned
    }

    /// Dispatch the Merge stage: `coord merge --repo <coord_repo>`.
    fn dispatch_pipeline_merge(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else { return false; };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else { return false; };
        let Some(coord_repo) = issue.coord_repo.clone() else {
            self.pipeline_status = Some((
                format!(
                    "no local repo mapping for {} — add it to coordinator.yml",
                    issue.repo_slug
                ),
                Instant::now(),
            ));
            return false;
        };
        let spawned = self.command_runner.spawn(&[
            "merge",
            "--repo",
            &coord_repo,
        ]);
        if spawned {
            self.pipeline_status = Some((
                format!("merge dispatched for {} (#{})", coord_repo, issue.number),
                Instant::now(),
            ));
        } else {
            self.pipeline_status = Some((
                "another command is running — try again in a moment".to_string(),
                Instant::now(),
            ));
        }
        spawned
    }

    /// Kick off a background `gh search issues` poll (no-op if one is
    /// already in flight or we polled less than 60 s ago).
    fn maybe_kick_pipeline_loader(&mut self) {
        if self.pipeline_loader.is_some() {
            return;
        }
        if let Some(t) = self.pipeline_last_load {
            if t.elapsed() < Duration::from_secs(60) {
                return;
            }
        }
        if self.data.pipeline_tracked_labels.is_empty() {
            return;
        }
        let labels = self.data.pipeline_tracked_labels.clone();
        let repos = self.data.pipeline_repos.clone();
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let result = fetch_pipeline_issues(&labels, &repos);
            let _ = tx.send(result);
        });
        self.pipeline_loader = Some(rx);
        self.pipeline_last_load = Some(Instant::now());
    }

    /// Kick off background `gh pr checks` polls for any PR in the merge
    /// queue without a fresh CI summary on hand.  No-op outside the Pipeline
    /// view; entries fetched within the last 30 s are skipped.  Each poll
    /// runs in its own thread so the UI is never blocked on `gh`.
    fn maybe_kick_ci_check_loaders(&mut self) {
        if self.active_view != SidebarView::Pipeline {
            return;
        }
        let stale = Duration::from_secs(30);
        // Snapshot the queue so we don't hold a borrow across mutable self.
        let targets: Vec<(String, i64)> = self
            .data
            .merge_queue
            .iter()
            .filter_map(|m| {
                let pr = m.pr_number?;
                if m.repo_github.is_empty() {
                    return None;
                }
                Some((m.repo_github.clone(), pr))
            })
            .collect();
        for key in targets {
            if self.pipeline_ci_loader.contains_key(&key) {
                continue;
            }
            if let Some(existing) = self.pipeline_ci_checks.get(&key) {
                if existing.fetched_at.elapsed() < stale {
                    continue;
                }
            }
            let (tx, rx) = std::sync::mpsc::channel();
            let repo = key.0.clone();
            let pr = key.1;
            std::thread::spawn(move || {
                let _ = tx.send(fetch_ci_check_summary(&repo, pr));
            });
            self.pipeline_ci_loader.insert(key, rx);
        }
    }

    /// Drain completed CI-check fetches into `pipeline_ci_checks`.  Returns
    /// `true` when at least one summary changed (caller redraws).
    fn poll_ci_check_loaders(&mut self) -> bool {
        let keys: Vec<(String, i64)> = self.pipeline_ci_loader.keys().cloned().collect();
        let mut changed = false;
        for key in keys {
            let result = self
                .pipeline_ci_loader
                .get(&key)
                .and_then(|rx| rx.try_recv().ok());
            let Some(result) = result else {
                continue;
            };
            self.pipeline_ci_loader.remove(&key);
            if let Ok(summary) = result {
                self.pipeline_ci_checks.insert(key, summary);
                changed = true;
            }
            // On error we leave any prior summary in place — a transient
            // `gh` failure shouldn't blank the row.
        }
        changed
    }

    /// #253: True when the currently-selected pipeline issue has a queued
    /// merge entry whose work assignment lacks an approved review on the
    /// board.  Drives the status-bar swap so the user sees the block before
    /// pressing `m`.
    ///
    /// Mirrors the Python `requires_review` + `has_approved_review` logic
    /// (coord/merge_queue.py) on the data the TUI loads from the local DB.
    /// Returns false when reviews aren't configured (no review-typed
    /// assignment ever appeared for this issue) so the hint doesn't appear
    /// in projects that don't use the review pipeline.
    fn merge_blocked_on_review_for_selected_issue(&self) -> bool {
        if self.active_view != SidebarView::Pipeline {
            return false;
        }
        let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i))
        else { return false; };
        // Need a queued merge entry that is not yet merged.
        let entry = self
            .data
            .merge_queue
            .iter()
            .find(|m| m.issue_number == Some(issue.number));
        let Some(entry) = entry else { return false; };
        if entry.state == "merged" {
            return false;
        }
        // Only consider issues whose pipeline includes a "review" stage —
        // a project without review gates shouldn't see this hint.
        let stages = self.pipeline_stage_names();
        if !stages.iter().any(|s| s == "review") {
            return false;
        }
        // Find the latest work assignment for this issue (matching the
        // merge entry's assignment_id).  Then look for an approve verdict
        // on a review-typed assignment whose review_of_assignment_id
        // matches.  Mirrors coord/merge_queue.py::has_approved_review.
        let work_id = &entry.assignment_id;
        let approved = self.data.assignments.iter().any(|a| {
            a.assignment_type.as_deref() == Some("review")
                && a.review_of_assignment_id.as_deref() == Some(work_id)
                && a.review_verdict.as_deref() == Some("approve")
        });
        !approved
    }

    /// Classification of whether a Merge action on the currently-selected
    /// Pipeline row will actually do anything useful.  Drives both the
    /// dispatch path (so silent no-ops are replaced with actionable
    /// toasts) and the Merge button's `enabled` state on the panel
    /// toolbar.
    fn pipeline_merge_state(&self) -> PipelineMergeState {
        if self.active_view != SidebarView::Pipeline {
            return PipelineMergeState::NotApplicable;
        }
        let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i))
        else {
            return PipelineMergeState::NotApplicable;
        };
        let Some(entry) = self
            .data
            .merge_queue
            .iter()
            .find(|m| m.issue_number == Some(issue.number))
        else {
            return PipelineMergeState::NoQueue {
                issue: issue.number,
            };
        };
        if entry.state == "merged" {
            return PipelineMergeState::Merged {
                issue: issue.number,
            };
        }
        // Review gate — only meaningful when the pipeline has a Review
        // stage; otherwise downstream `coord merge` won't enforce it.
        let stages = self.pipeline_stage_names();
        if stages.iter().any(|s| s == "review") {
            let work_id = &entry.assignment_id;
            // Find the review assignment paired with this work.
            let verdict: Option<&str> = self
                .data
                .assignments
                .iter()
                .filter(|a| a.assignment_type.as_deref() == Some("review"))
                .filter(|a| a.review_of_assignment_id.as_deref() == Some(work_id))
                .find_map(|a| a.review_verdict.as_deref());
            match verdict {
                Some("approve") => { /* good — fall through to CI check */ }
                Some(other) => {
                    return PipelineMergeState::BlockedOnReview {
                        issue: issue.number,
                        verdict: other.to_string(),
                    };
                }
                None => {
                    return PipelineMergeState::BlockedOnReview {
                        issue: issue.number,
                        verdict: "pending".to_string(),
                    };
                }
            }
        }
        // CI gate — only meaningful when a summary has been fetched
        // and we've actually seen failures.  Pending checks fall
        // through to Ready (coord merge will block server-side).
        if let Some(summary) = self.ci_summary_for_selected_issue() {
            if summary.has_failures() {
                return PipelineMergeState::BlockedOnCi {
                    issue: issue.number,
                    repo: issue.coord_repo.clone().unwrap_or_default(),
                };
            }
        }
        PipelineMergeState::Ready {
            issue: issue.number,
            repo: issue.coord_repo.clone().unwrap_or_default(),
        }
    }

    /// #272-followup: route a Merge action (m keybind OR toolbar:merge
    /// button) through the state classifier so silent no-ops are
    /// replaced with actionable toasts.  Returns `true` when something
    /// happened (dispatched, opened a prompt, or surfaced a toast) so
    /// the caller can request a redraw.
    fn dispatch_pipeline_merge_for_selected_issue(&mut self) -> bool {
        match self.pipeline_merge_state() {
            PipelineMergeState::NotApplicable => {
                // Outside the Pipeline view, fall back to the unscoped
                // "merge whatever's ready" behaviour (matches the
                // pre-classifier `m` keybind on the Board / Machines
                // views).
                self.command_runner.spawn(&["merge"]);
                true
            }
            PipelineMergeState::NoQueue { issue } => {
                self.push_toast(
                    "Merge",
                    &format!(
                        "#{issue}: no PR queued yet — work hasn't pushed a branch."
                    ),
                    ToastSeverity::Warning,
                );
                true
            }
            PipelineMergeState::Merged { issue } => {
                self.push_toast(
                    "Merge",
                    &format!("#{issue} is already merged."),
                    ToastSeverity::Info,
                );
                true
            }
            PipelineMergeState::BlockedOnReview { issue, verdict } => {
                let summary = match verdict.as_str() {
                    "request-changes" => "review requested changes",
                    "pending" => "no review verdict yet",
                    other => other,
                };
                self.push_toast(
                    "Merge blocked",
                    &format!(
                        "#{issue}: {summary}. Press M to skip review, or R to re-dispatch the reviewer."
                    ),
                    ToastSeverity::Warning,
                );
                true
            }
            PipelineMergeState::BlockedOnCi { issue, repo } => {
                // Same confirm prompt the standalone CI-failed `m`
                // keybind opens; the user has to type y to bypass.
                let _ = issue;
                self.pending_force_merge = Some(repo);
                true
            }
            PipelineMergeState::Ready { issue: _, repo } => {
                if repo.is_empty() {
                    // No coord_repo mapping — fall back to unscoped
                    // merge so power users with cross-repo work can
                    // still drive it from the TUI.
                    self.command_runner.spawn(&["merge"]);
                } else {
                    self.command_runner
                        .spawn(&["merge", "--repo", &repo]);
                }
                true
            }
        }
    }

    /// Find the most-recent review assignment id for the selected Pipeline
    /// row whose verdict is `request-changes`.  Used by the Fix action
    /// (action bar + right-click menu + F keybind) to identify which
    /// review's findings the dispatched fix worker should address.
    fn selected_pipeline_review_id_for_bounce(&self) -> Option<String> {
        if self.active_view != SidebarView::Pipeline {
            return None;
        }
        let issue = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i))?;
        let entry = self
            .data
            .merge_queue
            .iter()
            .find(|m| m.issue_number == Some(issue.number))?;
        let work_id = entry.assignment_id.clone();
        // Most-recent review (highest dispatched_at) paired with this
        // work and carrying a request-changes verdict.
        self.data
            .assignments
            .iter()
            .filter(|a| a.assignment_type.as_deref() == Some("review"))
            .filter(|a| a.review_of_assignment_id.as_deref() == Some(&work_id))
            .filter(|a| a.review_verdict.as_deref() == Some("request-changes"))
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .map(|a| a.id.clone())
    }

    /// Dispatch `coord bounce <review-id>` for the selected Pipeline row.
    /// Returns `true` when a command was actually spawned; toasts and
    /// returns `false` when the row isn't actionable (no
    /// request-changes verdict, no review pairing, etc.).
    fn dispatch_bounce_for_selected_pipeline_row(&mut self) -> bool {
        let Some(review_id) = self.selected_pipeline_review_id_for_bounce() else {
            self.push_toast(
                "Bounce",
                "No request-changes review found for this row — nothing to address.",
                ToastSeverity::Warning,
            );
            return false;
        };
        let spawned = self.command_runner.spawn(&["bounce", &review_id]);
        if spawned {
            self.push_toast(
                "Bounce",
                &format!(
                    "Dispatching fix worker for review {}\u{2026} Work will go Active in 1-3s once the subprocess completes.",
                    &review_id[..review_id.len().min(8)],
                ),
                ToastSeverity::Info,
            );
        } else {
            self.push_toast(
                "Bounce",
                "Another command is running — try again in a moment.",
                ToastSeverity::Warning,
            );
        }
        spawned
    }

    /// Look up the CI summary for the currently-selected pipeline issue's
    /// merge queue entry.  Returns `None` when no PR is queued for the
    /// selected issue, or when no summary has been fetched yet.
    fn ci_summary_for_selected_issue(&self) -> Option<&CiCheckSummary> {
        let issue = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i))?;
        let entry = self
            .data
            .merge_queue
            .iter()
            .find(|m| m.issue_number == Some(issue.number))?;
        let pr = entry.pr_number?;
        self.pipeline_ci_checks
            .get(&(entry.repo_github.clone(), pr))
    }

    /// Drain the in-flight poll channel without blocking. Returns `true`
    /// when results were received and the issue list/sidebar changed.
    fn poll_pipeline_loader(&mut self) -> bool {
        let rx = match &self.pipeline_loader {
            Some(rx) => rx,
            None => return false,
        };
        match rx.try_recv() {
            Ok(PipelineLoaderResult::Ok(issues)) => {
                // Capture the currently-selected issue's identity BEFORE
                // we replace pipeline_issues — otherwise the old pipeline_sel
                // index is looked up against the new list and resolves to
                // the wrong issue (or None), defaulting the selection back
                // to issue #0 on every 15s refresh.
                let prev_sel = self.capture_pipeline_selection_id();
                self.pipeline_issues = issues;
                self.pipeline_loader = None;
                self.rebuild_pipeline_sidebar(prev_sel);
                true
            }
            Ok(PipelineLoaderResult::Err(msg)) => {
                self.pipeline_status = Some((format!("gh: {}", msg), Instant::now()));
                self.pipeline_loader = None;
                true
            }
            Err(std::sync::mpsc::TryRecvError::Empty) => false,
            Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                self.pipeline_loader = None;
                false
            }
        }
    }

    /// Pipeline panel detail-side: list-style fallback when no PipelineView
    /// can be drawn yet (no issue selected / still loading).
    fn pipeline_placeholder_list(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();
        if self.pipeline_loader.is_some() && self.pipeline_issues.is_empty() {
            items.push(kv_item(
                "",
                "  Loading tracked issues from GitHub...",
                Some(Color::rgb(180, 180, 100)),
            ));
        } else if self.pipeline_issues.is_empty() {
            let labels = self.data.pipeline_tracked_labels.join(", ");
            items.push(kv_item(
                "",
                &format!(
                    "  No issues found with label(s): {}",
                    if labels.is_empty() { "(none)".into() } else { labels }
                ),
                Some(Color::rgb(140, 140, 140)),
            ));
            items.push(kv_item(
                "",
                "  Press 'r' to refresh, or label issues with 'coord' on GitHub.",
                Some(Color::rgb(100, 100, 100)),
            ));
        } else if let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i)) {
            // An issue is selected but the widget was suppressed (closed-no-pipeline).
            if issue.is_closed && !self.issue_has_any_assignment(issue) {
                items.push(kv_item(
                    "",
                    "  Closed without coord pipeline — no stages tracked.",
                    Some(Color::rgb(120, 180, 120)),
                ));
            } else {
                items.push(kv_item(
                    "",
                    "  Select an issue on the left to see its pipeline.",
                    Some(Color::rgb(140, 140, 140)),
                ));
            }
        } else {
            items.push(kv_item(
                "",
                "  Select an issue on the left to see its pipeline.",
                Some(Color::rgb(140, 140, 140)),
            ));
        }
        ListView {
            id: WidgetId::new("pipeline-empty"),
            title: Some(StyledText::plain(" PIPELINE ")),
            items,
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: false,
            bordered: false,
        }
    }

    fn board_detail_tab_bar(&self) -> TabBar {
        TabBar {
            id: WidgetId::new("board-detail-tabs"),
            tabs: vec![
                TabItem {
                    label: " Board ".to_string(),
                    is_active: self.board_detail_tab == BoardDetailTab::Board,
                    is_dirty: false,
                    is_preview: false,
                },
                TabItem {
                    label: " Issue ".to_string(),
                    is_active: self.board_detail_tab == BoardDetailTab::Issue,
                    is_dirty: false,
                    is_preview: false,
                },
            ],
            scroll_offset: 0,
            right_segments: vec![],
            active_accent: None,
            show_tab_close: false,
            compact: true,
        }
    }

    /// Look up the selected board issue's body and render via the shared
    /// `issue_body_list` helper. Layered lookup (#168 motivated this):
    ///
    /// 1. Synced row in `data.open_issues` — fast path, no I/O.
    /// 2. In-memory `fetched_issues_cache` populated by a prior background
    ///    `gh issue view` for this session.
    /// 3. In-flight background fetch — show a "Fetching…" placeholder and
    ///    let the next render pick up the result.
    /// 4. No data yet — spawn `gh issue view` in the background (writes the
    ///    result through to the local `issues` table on success so future
    ///    sessions don't re-fetch) and show a placeholder.
    fn board_issue_body_list(&self) -> ListView {
        let repo = self.board_active_repo().map(str::to_string);
        let group = self.board_selected_issue_group().cloned();
        let (Some(repo), Some(g)) = (repo, group) else {
            return issue_body_list(None, self.detail_scroll, "board-issue-body");
        };
        let key = (repo.clone(), g.issue_number);

        // 1. Synced row.
        if let Some(oi) = self
            .data
            .open_issues
            .iter()
            .find(|oi| oi.repo_name == repo && oi.number == g.issue_number)
        {
            return issue_body_list(
                Some((oi.number, oi.title.as_str(), oi.body.as_str(), &oi.labels[..])),
                self.detail_scroll,
                "board-issue-body",
            );
        }

        // 2. Drain any completed background fetch into the cache so step 3 picks it up.
        let pending_result = {
            let pending = self.pending_issue_fetches.borrow();
            pending.get(&key).map(|rx| rx.try_recv())
        };
        if let Some(recv) = pending_result {
            match recv {
                Ok(Ok(fetched)) => {
                    self.pending_issue_fetches.borrow_mut().remove(&key);
                    self.fetched_issues_cache
                        .borrow_mut()
                        .insert(key.clone(), fetched);
                }
                Ok(Err(_)) | Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                    // Fetch finished with an error or the thread died — drop
                    // the receiver so the cold-path below will re-spawn next
                    // render. Error surfaces below as the placeholder.
                    self.pending_issue_fetches.borrow_mut().remove(&key);
                }
                Err(std::sync::mpsc::TryRecvError::Empty) => {} // still in flight
            }
        }

        // 3. In-memory cache (populated by a completed fetch).
        if let Some(f) = self.fetched_issues_cache.borrow().get(&key).cloned() {
            return issue_body_list(
                Some((f.number, f.title.as_str(), f.body.as_str(), &f.labels[..])),
                self.detail_scroll,
                "board-issue-body",
            );
        }

        // 4. Spawn if no fetch is already running.
        if !self.pending_issue_fetches.borrow().contains_key(&key) {
            // Resolve the GitHub slug for this repo. If we can't, fall back to
            // the title-only placeholder instead of a broken gh call.
            let slug = self
                .data
                .pipeline_repos
                .iter()
                .find(|(local, _)| local == &repo)
                .map(|(_, slug)| slug.clone());
            if let Some(slug) = slug {
                let rx = spawn_issue_fetch(slug, repo.clone(), g.issue_number);
                self.pending_issue_fetches.borrow_mut().insert(key.clone(), rx);
            } else {
                // No slug → can't fetch. Show the title we have with a hint.
                return issue_body_list(
                    Some((
                        g.issue_number,
                        g.issue_title.as_str(),
                        "(no GitHub slug for this repo — add it to coordinator.yml.repos[].github)",
                        &[][..],
                    )),
                    self.detail_scroll,
                    "board-issue-body",
                );
            }
        }

        // Placeholder while fetch is in flight.
        issue_body_list(
            Some((
                g.issue_number,
                g.issue_title.as_str(),
                "(fetching body via `gh issue view`…)",
                &[][..],
            )),
            self.detail_scroll,
            "board-issue-body",
        )
    }

    fn pipeline_detail_tab_bar(&self) -> TabBar {
        TabBar {
            id: WidgetId::new("pipeline-detail-tabs"),
            tabs: vec![
                TabItem {
                    label: " Pipeline ".to_string(),
                    is_active: self.pipeline_detail_tab == PipelineDetailTab::Pipeline,
                    is_dirty: false,
                    is_preview: false,
                },
                TabItem {
                    label: " Issue ".to_string(),
                    is_active: self.pipeline_detail_tab == PipelineDetailTab::Issue,
                    is_dirty: false,
                    is_preview: false,
                },
                TabItem {
                    label: " Stages ".to_string(),
                    is_active: self.pipeline_detail_tab == PipelineDetailTab::Stages,
                    is_dirty: false,
                    is_preview: false,
                },
            ],
            scroll_offset: 0,
            right_segments: vec![],
            active_accent: None,
            show_tab_close: false,
            compact: true,
        }
    }

    /// Issue tab: title header + scrollable full body (j/k to scroll).
    fn pipeline_issue_body_list(&self) -> ListView {
        let issue = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i));
        issue_body_list(
            issue.map(|i| (i.number, i.title.as_str(), i.body.as_str(), &i.all_labels[..])),
            self.pipeline_detail_scroll,
            "pipeline-issue-body",
        )
    }

    /// Pipeline tab: meta strip (repo/labels/gates/status) plus
    /// #271 part 2 test guidance (branch / repo path / suggested
    /// commands / persisted Phase 1 build result) when Test is
    /// actionable or has been built.
    fn pipeline_issue_summary(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();
        if let Some(idx) = self.pipeline_sel {
            if let Some(issue) = self.pipeline_issues.get(idx) {
                items.push(kv_item("Repo", &issue.repo_slug, Some(Color::rgb(160, 160, 180))));
                if let Some(local) = &issue.coord_repo {
                    items.push(kv_item("Local", local, Some(Color::rgb(140, 200, 140))));
                } else {
                    items.push(kv_item("Local", "(no coordinator.yml mapping)", Some(Color::rgb(220, 150, 80))));
                }
                if !issue.matched_labels.is_empty() {
                    items.push(kv_item("Labels", &issue.matched_labels.join(", "), Some(Color::rgb(160, 160, 180))));
                }
                items.push(kv_item(
                    "Gates",
                    &self.pipeline_stage_names_for_issue(issue).join(" → "),
                    Some(Color::rgb(160, 160, 180)),
                ));
                if let Some((msg, when)) = &self.pipeline_status {
                    if when.elapsed() < Duration::from_secs(8) {
                        items.push(kv_item("", "", None));
                        items.push(kv_item("", &format!("  {}", msg), Some(Color::rgb(180, 180, 100))));
                    }
                }

                // #271 part 2: surface test guidance + persisted build
                // result inline.  Both rely on having a Work assignment
                // to anchor against; without one there's nothing to
                // test or build.
                self.append_test_guidance_rows(&mut items, issue);
            }
        }
        ListView {
            id: WidgetId::new("pipeline-summary"),
            title: None,
            items,
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: false,
            bordered: false,
        }
    }

    /// #271 part 2: append a "Test guidance" block — branch, local
    /// path, last-build outcome (persisted), suggested next commands —
    /// when the user is looking at an issue whose Test stage is in
    /// play (actionable or recently built).
    fn append_test_guidance_rows(
        &self,
        items: &mut Vec<ListItem>,
        issue: &PipelineIssue,
    ) {
        // Find the latest Work assignment for this issue (the build
        // hangs off its branch).
        let work = self.assignments_for_stage(issue, "work");
        let latest = work.iter().max_by(|a, b| {
            a.dispatched_at
                .partial_cmp(&b.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        let Some(latest) = latest else { return; };
        // Show this block ONLY when the Test stage is in play (Active
        // or Pending with Work done).  Skip for issues that aren't at
        // the test step yet.
        let test_status = self.test_stage_status_for(issue);
        let actionable = self.test_gate_actionable();
        let has_build_record = self.last_test_builds.contains_key(&latest.id);
        let in_flight = self.test_build_in_flight(&latest.id);
        if !actionable && !has_build_record && !in_flight {
            return;
        }

        items.push(kv_item("", "", None));
        items.push(kv_item(
            "Test",
            "ready for manual verification",
            Some(Color::rgb(160, 200, 220)),
        ));
        if let Some(branch) = &latest.branch {
            items.push(kv_item(
                "  Branch",
                branch,
                Some(Color::rgb(160, 160, 180)),
            ));
        }
        // Local repo path — only when we have a coord-repo mapping.
        if let Some(local) = &issue.coord_repo {
            items.push(kv_item(
                "  Path",
                &format!("see coordinator.yml `repos`: {}", local),
                Some(Color::rgb(160, 160, 180)),
            ));
        }
        // Persistent build status.  Three states surfaced.
        if in_flight {
            // The job is still going; show elapsed.
            if let Some(job) = self.test_build_jobs.get(&latest.id) {
                let elapsed = job.started_at.elapsed().as_secs();
                items.push(kv_item(
                    "  Build",
                    &format!("running ({elapsed}s elapsed) — log {}", job.log_path.display()),
                    Some(Color::rgb(220, 180, 100)),
                ));
            }
        } else if let Some(last) = self.last_test_builds.get(&latest.id) {
            let ago = last.finished_at.elapsed().as_secs();
            // Show the branch the build was actually run against —
            // useful when the user has fix-iterated since (the work
            // assignment's branch may have advanced).
            let branch_note = if Some(&last.branch) != latest.branch.as_ref() {
                format!(" on {}", last.branch)
            } else {
                String::new()
            };
            if last.exit_code == 0 {
                items.push(kv_item(
                    "  Build",
                    &format!(
                        "✓ succeeded in {}s ({}s ago{}) — log {}",
                        last.duration_secs, ago, branch_note, last.log_path.display()
                    ),
                    Some(Color::rgb(120, 200, 120)),
                ));
            } else {
                let snippet = if last.first_error.is_empty() {
                    String::new()
                } else {
                    let trimmed: String = last.first_error.chars().take(80).collect();
                    if last.first_error.chars().count() > 80 {
                        format!("{}…", trimmed)
                    } else {
                        trimmed
                    }
                };
                items.push(kv_item(
                    "  Build",
                    &format!(
                        "✗ exit {} ({}s ago{}){} — log {}",
                        last.exit_code,
                        ago,
                        branch_note,
                        if snippet.is_empty() {
                            String::new()
                        } else {
                            format!(": {snippet}")
                        },
                        last.log_path.display(),
                    ),
                    Some(Color::rgb(220, 100, 100)),
                ));
            }
            // Issue-number breadcrumb, useful when scrolling back via
            // log files — the issue number is the human-friendly key.
            let _ = last.issue_number; // anchored in `last` for future use
        } else {
            items.push(kv_item(
                "  Build",
                "(not run yet — press B)",
                Some(Color::rgb(160, 160, 180)),
            ));
        }
        // #271 part 2 follow-up: surface the PR description and files
        // changed inline when a PR exists.  The worker's PR body is
        // the canonical place they explain new sample apps, demo
        // binaries, manual test steps — without this the user had to
        // ask Claude separately.
        if let Some(pr_number) = self.pipeline_pr_number(issue) {
            items.push(kv_item(
                "  PR",
                &format!("#{}", pr_number),
                Some(Color::rgb(160, 200, 220)),
            ));
            match self.pr_info_for_issue(issue) {
                Some(pr) => {
                    if !pr.title.is_empty() {
                        items.push(kv_item(
                            "  PR title",
                            &pr.title,
                            Some(Color::rgb(220, 220, 220)),
                        ));
                    }
                    // Show up to 6 body lines; the user can open the PR
                    // for the rest.  Skip empty lines at the head so
                    // the preview is dense.
                    let body_lines: Vec<&str> = pr
                        .body
                        .lines()
                        .skip_while(|l| l.trim().is_empty())
                        .take(6)
                        .collect();
                    if !body_lines.is_empty() {
                        items.push(kv_item(
                            "  PR notes",
                            "",
                            Some(Color::rgb(160, 160, 180)),
                        ));
                        for line in body_lines {
                            // Truncate any wildly long line so the
                            // single-row list doesn't blow out.
                            let trimmed: String = line.chars().take(140).collect();
                            items.push(kv_item(
                                "",
                                &format!("    {trimmed}"),
                                Some(Color::rgb(200, 200, 200)),
                            ));
                        }
                        if pr.body.lines().count() > 6 {
                            items.push(kv_item(
                                "",
                                "    …",
                                Some(Color::rgb(140, 140, 160)),
                            ));
                        }
                    }
                    // Files-changed list — useful for "what should I
                    // test?" — capped at the first 10 entries.
                    if !pr.files.is_empty() {
                        items.push(kv_item(
                            "  Files",
                            &format!("({} changed)", pr.files.len()),
                            Some(Color::rgb(160, 160, 180)),
                        ));
                        for path in pr.files.iter().take(10) {
                            items.push(kv_item(
                                "",
                                &format!("    {path}"),
                                Some(Color::rgb(200, 200, 200)),
                            ));
                        }
                        if pr.files.len() > 10 {
                            items.push(kv_item(
                                "",
                                &format!("    … and {} more", pr.files.len() - 10),
                                Some(Color::rgb(140, 140, 160)),
                            ));
                        }
                    }
                    // The latest substantive review (state != PENDING,
                    // non-empty body when possible).  Filters out
                    // "COMMENTED" reviews with empty bodies that gh
                    // sometimes returns from sidecar bots.
                    let latest_review = pr
                        .reviews
                        .iter()
                        .rev()
                        .find(|r| {
                            r.state != "PENDING"
                                && (!r.body.is_empty() || r.state == "APPROVED" || r.state == "CHANGES_REQUESTED")
                        });
                    if let Some(rev) = latest_review {
                        let (state_label, state_color) = match rev.state.as_str() {
                            "APPROVED" => ("✓ Approved", Color::rgb(120, 200, 120)),
                            "CHANGES_REQUESTED" => ("✗ Changes Requested", Color::rgb(220, 100, 100)),
                            "COMMENTED" => ("Commented", Color::rgb(160, 200, 220)),
                            other => (other, Color::rgb(200, 200, 200)),
                        };
                        items.push(kv_item("  Review", state_label, Some(state_color)));
                        // #248: surface the coord:review header counts as a
                        // single dense line when the coordinator embedded
                        // one.  Lets the user see "2 blocking, 5 polish"
                        // without scrolling the prose body.
                        if let Some(header) = parse_coord_review_header(&rev.body) {
                            let mut parts: Vec<String> = Vec::new();
                            if let Some(b) = header.blocking {
                                parts.push(format!("{b} blocking"));
                            }
                            if let Some(n) = header.nonblocking {
                                parts.push(format!("{n} non-blocking"));
                            }
                            if let Some(n) = header.nits {
                                parts.push(format!("{n} nits"));
                            }
                            if let Some(r) = header.reviewer.as_deref() {
                                parts.push(format!("reviewer: {r}"));
                            }
                            if !parts.is_empty() {
                                items.push(kv_item(
                                    "",
                                    &format!("    ({})", parts.join(", ")),
                                    Some(Color::rgb(160, 160, 180)),
                                ));
                            }
                        }
                        // Skip leading whitespace and the coord:review
                        // header HTML comment so the preview is dense
                        // and human-readable.
                        let body_lines: Vec<&str> = rev
                            .body
                            .lines()
                            .filter(|l| !l.trim_start().starts_with("<!-- coord:review"))
                            .skip_while(|l| l.trim().is_empty())
                            .take(10)
                            .collect();
                        for line in &body_lines {
                            let trimmed: String = line.chars().take(140).collect();
                            items.push(kv_item(
                                "",
                                &format!("    {trimmed}"),
                                Some(Color::rgb(200, 200, 200)),
                            ));
                        }
                        if rev.body.lines().count() > 10 {
                            items.push(kv_item(
                                "",
                                "    …",
                                Some(Color::rgb(140, 140, 160)),
                            ));
                        }
                    }
                }
                None => {
                    items.push(kv_item(
                        "  PR notes",
                        "(loading via gh pr view…)",
                        Some(Color::rgb(160, 160, 180)),
                    ));
                }
            }
        }

        // #252: worker-emitted smoke tests.  Three states (see
        // Assignment.smoke_tests doc): None → graceful placeholder,
        // empty list → "change is internal", non-empty → bullets.
        items.push(kv_item("", "", None));
        match latest.smoke_tests.as_deref() {
            Some(tests) if !tests.is_empty() => {
                items.push(kv_item(
                    "Smoke tests",
                    "(from worker)",
                    Some(Color::rgb(160, 200, 220)),
                ));
                for t in tests {
                    items.push(kv_item(
                        "",
                        &format!("  • {t}"),
                        Some(Color::rgb(220, 220, 220)),
                    ));
                }
            }
            Some(_empty) => {
                items.push(kv_item(
                    "Smoke tests",
                    "(none — worker reported change is internal)",
                    Some(Color::rgb(160, 160, 180)),
                ));
            }
            None => {
                items.push(kv_item(
                    "Smoke tests",
                    "(worker did not provide a list — inspect the diff)",
                    Some(Color::rgb(160, 160, 180)),
                ));
            }
        }

        // Suggested next steps.
        let test_label = match test_status {
            StageStatus::Failed => "previously failed — press R to re-dispatch Work, or P/F/S to re-record",
            _ => "press P=pass, F=fail, S=skip after manual verification",
        };
        items.push(kv_item(
            "  Next",
            test_label,
            Some(Color::rgb(160, 160, 180)),
        ));
    }

    /// Detail list for the Stages tab. One section per stage in the
    /// pipeline; under each section, the latest matching assignment's
    /// id, machine, status, dispatched/finished times and exit code
    /// (or the merge_queue row's state and PR for the merge stage).
    /// #stage-content: return the content rows for the focused stage's
    /// detail panel.  Each stage type sources its content differently:
    ///
    /// - **Plan**   — the worker's plan log tail (planning agent output)
    /// - **Work**   — the worker's log tail (summary of work done)
    /// - **Test**   — the cached `TestBuildResult.log_path` first 200 lines
    /// - **Review** — the cached `review_findings` body from the DB
    ///                (populated by notify when the review completed)
    /// - **Merge**  — the merge_queue entry's state + error if any
    ///
    /// Returns an empty `Vec` when no content can be sourced — the
    /// caller renders a "no content available" placeholder.
    fn stage_content_for(&self, issue: &PipelineIssue, stage_name: &str) -> Vec<ListItem> {
        match stage_name {
            "review" => self.stage_content_review(issue),
            "test" => self.stage_content_test(issue),
            "merge" => self.stage_content_merge(issue),
            // Plan + Work both read the latest assignment's log tail.
            "plan" | "work" => self.stage_content_assignment_log(issue, stage_name),
            _ => Vec::new(),
        }
    }

    /// Pull the cached review findings (verdict + body) for the
    /// selected pipeline issue.  Reads the JSON column populated by
    /// `coord/notify.py::_persist_review_findings`.
    fn stage_content_review(&self, issue: &PipelineIssue) -> Vec<ListItem> {
        // Find the latest review assignment for this issue.
        let local_repo = issue.coord_repo.as_deref();
        let review = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == r,
                None => true,
            })
            .filter(|a| a.assignment_type.as_deref() == Some("review"))
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            });
        let Some(review) = review else { return Vec::new() };
        // Findings JSON was loaded with the board (no per-render DB
        // query).  When None, notify hasn't parsed this review yet —
        // running `coord notify` or `coord bounce` refreshes it.
        let Some(raw) = review.review_findings.as_deref() else {
            return vec![kv_item(
                "",
                "   (review not yet parsed — run `coord notify` or `coord bounce` to refresh)",
                Some(Color::rgb(160, 160, 180)),
            )];
        };
        let parsed: serde_json::Value = match serde_json::from_str(raw) {
            Ok(v) => v,
            Err(_) => {
                return vec![kv_item(
                    "",
                    "   (review_findings JSON malformed — re-parse via `coord notify`)",
                    Some(Color::rgb(220, 180, 100)),
                )];
            }
        };
        let verdict = parsed
            .get("verdict")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown")
            .to_string();
        let body = parsed
            .get("body")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let mut rows: Vec<ListItem> = Vec::new();
        let (vtext, vcolor) = match verdict.as_str() {
            "approve" => ("✓ approved", Color::rgb(120, 200, 120)),
            "request-changes" => ("✗ changes requested", Color::rgb(220, 100, 100)),
            other => (other, Color::rgb(220, 180, 100)),
        };
        rows.push(kv_item("Verdict", vtext, Some(vcolor)));
        rows.push(kv_item("", "", None));
        // Render the body line-by-line as plain text (markdown
        // styling lands once quadraui#262 ships and we adopt it).
        // Filter out the coord:review header — that's machine-readable
        // metadata, not user-facing prose.
        for line in body
            .lines()
            .filter(|l| !l.trim_start().starts_with("<!-- coord:review"))
        {
            if line.is_empty() {
                rows.push(kv_item("", "", None));
            } else {
                let trimmed: String = line.chars().take(180).collect();
                rows.push(kv_item("", &format!("   {trimmed}"), None));
            }
        }
        rows
    }

    /// Test stage content — the cached build log from the most recent
    /// `coord test` run for the issue's work assignment.
    fn stage_content_test(&self, issue: &PipelineIssue) -> Vec<ListItem> {
        // Find the latest work assignment.
        let local_repo = issue.coord_repo.as_deref();
        let work_id = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == r,
                None => true,
            })
            .filter(|a| a.assignment_type.as_deref().unwrap_or("work") == "work")
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .map(|a| a.id.clone());
        let Some(work_id) = work_id else { return Vec::new() };
        let Some(build) = self.last_test_builds.get(&work_id) else {
            return vec![kv_item(
                "",
                "   (no build recorded — press B to run `coord test`)",
                Some(Color::rgb(160, 160, 180)),
            )];
        };
        let mut rows: Vec<ListItem> = Vec::new();
        let (status_label, status_color) = if build.exit_code == 0 {
            ("✓ succeeded", Color::rgb(120, 200, 120))
        } else {
            ("✗ failed", Color::rgb(220, 100, 100))
        };
        rows.push(kv_item("Build", status_label, Some(status_color)));
        rows.push(kv_item(
            "Exit code",
            &build.exit_code.to_string(),
            Some(Color::rgb(180, 180, 180)),
        ));
        rows.push(kv_item(
            "Log",
            &build.log_path.display().to_string(),
            Some(Color::rgb(160, 160, 180)),
        ));
        rows.push(kv_item("", "", None));
        // Read the first 200 lines of the log for inline display.
        let content = std::fs::read_to_string(&build.log_path).unwrap_or_default();
        if content.is_empty() {
            rows.push(kv_item(
                "",
                "   (log file empty or unreadable)",
                Some(Color::rgb(160, 160, 180)),
            ));
        } else {
            for line in content.lines().take(200) {
                let trimmed: String = line.chars().take(180).collect();
                rows.push(kv_item("", &format!("   {trimmed}"), None));
            }
        }
        rows
    }

    /// Merge stage content — pulled from the merge_queue entry.
    fn stage_content_merge(&self, issue: &PipelineIssue) -> Vec<ListItem> {
        let entry = self
            .data
            .merge_queue
            .iter()
            .find(|m| m.issue_number == Some(issue.number));
        let Some(entry) = entry else { return Vec::new() };
        let mut rows: Vec<ListItem> = Vec::new();
        rows.push(kv_item(
            "State",
            &entry.state,
            Some(Color::rgb(200, 200, 220)),
        ));
        if let Some(pr) = entry.pr_number {
            rows.push(kv_item(
                "PR",
                &format!("#{pr}"),
                Some(Color::rgb(160, 200, 220)),
            ));
        }
        rows
    }

    /// Plan / Work stage content — read the tail of the matching
    /// assignment's log file.  Returns an empty placeholder when no
    /// assignment exists or the log is unreadable.
    fn stage_content_assignment_log(
        &self,
        issue: &PipelineIssue,
        stage: &str,
    ) -> Vec<ListItem> {
        let local_repo = issue.coord_repo.as_deref();
        let assignment = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == r,
                None => true,
            })
            .filter(|a| {
                let t = a.assignment_type.as_deref().unwrap_or("work");
                if stage == "work" {
                    t == "work"
                } else {
                    t == stage
                }
            })
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            });
        let Some(a) = assignment else { return Vec::new() };
        let log_path = std::path::PathBuf::from(
            std::env::var("HOME").unwrap_or_default(),
        )
        .join(".coord")
        .join("logs")
        .join(format!("{}.log", a.id));
        let content = std::fs::read_to_string(&log_path).unwrap_or_default();
        if content.is_empty() {
            return vec![kv_item(
                "",
                &format!(
                    "   (log not on this machine — assignment ran on {}; \
                     run `coord log {} -f` to follow)",
                    a.machine, a.id,
                ),
                Some(Color::rgb(160, 160, 180)),
            )];
        }
        // Tail of the log — last 200 lines.
        let lines: Vec<&str> = content.lines().collect();
        let tail_start = lines.len().saturating_sub(200);
        let mut rows: Vec<ListItem> = Vec::new();
        if tail_start > 0 {
            rows.push(kv_item(
                "",
                &format!(
                    "   (showing last 200 of {} lines from {}.log)",
                    lines.len(), a.id,
                ),
                Some(Color::rgb(140, 140, 160)),
            ));
            rows.push(kv_item("", "", None));
        }
        for line in &lines[tail_start..] {
            let trimmed: String = line.chars().take(180).collect();
            rows.push(kv_item("", &format!("   {trimmed}"), None));
        }
        rows
    }

    fn pipeline_stages_list(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();
        let issue = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))
            .cloned();
        let Some(issue) = issue else {
            items.push(kv_item("", "(no issue selected)", Some(Color::rgb(140, 140, 140))));
            return ListView {
                id: WidgetId::new("pipeline-stages"),
                title: None,
                items,
                selected_idx: 0,
                scroll_offset: 0,
                has_focus: false,
                bordered: false,
            };
        };

        // Closed issue with no assignment rows → no stage rows to show.
        if issue.is_closed && !self.issue_has_any_assignment(&issue) {
            items.push(kv_item(
                "",
                "  Closed without coord pipeline — no stages tracked.",
                Some(Color::rgb(120, 180, 120)),
            ));
            return ListView {
                id: WidgetId::new("pipeline-stages"),
                title: None,
                items,
                selected_idx: 0,
                scroll_offset: 0,
                has_focus: false,
                bordered: false,
            };
        }

        let stage_names = self.pipeline_stage_names_for_issue(&issue);
        for name in &stage_names {
            let status = self.stage_status_for(&issue, name);
            let (icon, color) = match status {
                StageStatus::Done => ("✓", Color::rgb(120, 200, 120)),
                StageStatus::Active => ("~", Color::rgb(220, 180, 100)),
                StageStatus::Failed => ("✗", Color::rgb(220, 70, 70)),
                StageStatus::Skipped => ("─", Color::rgb(140, 140, 140)),
                StageStatus::Pending => ("·", Color::rgb(140, 140, 140)),
                StageStatus::Stale => ("↻", Color::rgb(140, 140, 140)),
            };
            let header = format!(" {} {}", icon, capitalize(name));
            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(header, color)],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Normal,
            });

            if name == "merge" {
                self.append_merge_stage_rows(&mut items, &issue);
            } else {
                self.append_assignment_stage_rows(&mut items, &issue, name);
            }
            items.push(kv_item("", "", None));
        }

        // #stage-content: per-stage content panel.  When a stage is
        // focused (mouse click on a stage box OR [/] keys on the
        // Stages tab), render its associated output at the bottom of
        // the list.  Plan → planner agent output; Work → worker log
        // tail; Test → build output; Review → cached review findings.
        if let Some(focused_idx) = self
            .pipeline_focused_stage
            .filter(|&i| i < stage_names.len())
        {
            let name = &stage_names[focused_idx];
            items.push(kv_item("", "", None));
            items.push(kv_item(
                "",
                &format!(" ── Stage content: {} ──", capitalize(name)),
                Some(Color::rgb(220, 220, 230)),
            ));
            items.push(kv_item(
                "",
                "   ([/] previous · ]/] next · click a stage box to switch)",
                Some(Color::rgb(140, 140, 160)),
            ));
            items.push(kv_item("", "", None));
            let content_rows = self.stage_content_for(&issue, name);
            if content_rows.is_empty() {
                items.push(kv_item(
                    "",
                    "   (no content available for this stage yet)",
                    Some(Color::rgb(140, 140, 160)),
                ));
            } else {
                items.extend(content_rows);
            }
        } else {
            // No stage focused — hint at the affordance once the user
            // has rendered at least one stage.
            items.push(kv_item(
                "",
                " Tip: click a stage box (or press [ / ]) to view its output here.",
                Some(Color::rgb(140, 140, 160)),
            ));
        }
        ListView {
            id: WidgetId::new("pipeline-stages"),
            title: None,
            items,
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: false,
            bordered: false,
        }
    }

    /// Push detail rows for a non-merge stage's matching assignments
    /// (most-recent first). Falls back to a single "(not started)" row.
    fn append_assignment_stage_rows(
        &self,
        items: &mut Vec<ListItem>,
        issue: &PipelineIssue,
        stage: &str,
    ) {
        let local_repo = issue.coord_repo.as_deref();
        let plan_gate_on = self.data.pipeline_require_plan;
        let has_plan_for_issue = self.issue_has_plan_assignment(issue);
        let matching: Vec<&Assignment> = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == r,
                None => true,
            })
            .filter(|a| {
                let t = a.assignment_type.as_deref().unwrap_or("work");
                // Legacy fold: plan-typed assignments count as Work only
                // when this issue's strip has no Plan stage (no plan
                // gate globally AND no plan-typed assignment exists).
                if stage == "work" && !plan_gate_on && !has_plan_for_issue {
                    t == "work" || t == "plan"
                } else {
                    t == stage
                }
            })
            .collect();

        if matching.is_empty() {
            items.push(kv_item("", "    (not started)", Some(Color::rgb(140, 140, 140))));
            return;
        }
        for a in matching.iter() {
            let id_short = if a.id.len() > 8 { &a.id[..8] } else { &a.id };
            items.push(kv_item("Assignment", id_short, Some(Color::rgb(160, 200, 220))));
            items.push(kv_item("Machine", &a.machine, Some(Color::rgb(210, 210, 210))));
            let status_color = match a.status.as_str() {
                "running" => Color::rgb(220, 180, 100),
                "done" => Color::rgb(120, 200, 120),
                "failed" => Color::rgb(220, 70, 70),
                _ => Color::rgb(180, 180, 180),
            };
            items.push(kv_item("Status", &a.status, Some(status_color)));
            if let Some(branch) = &a.branch {
                items.push(kv_item("Branch", branch, Some(Color::rgb(200, 200, 200))));
            }
            if let Some(model) = &a.model {
                items.push(kv_item("Model", model, Some(Color::rgb(180, 180, 180))));
            }
            if let Some(t) = a.dispatched_at {
                items.push(kv_item("Dispatched", &format_unix_time(t), Some(Color::rgb(180, 180, 180))));
            }
            if let Some(t) = a.finished_at {
                items.push(kv_item("Finished", &format_unix_time(t), Some(Color::rgb(180, 180, 180))));
            }
            if let Some(ec) = a.exit_code {
                let ec_color = if ec == 0 {
                    Color::rgb(120, 200, 120)
                } else {
                    Color::rgb(220, 70, 70)
                };
                items.push(kv_item("Exit code", &ec.to_string(), Some(ec_color)));
            }
            // #208: surface captured worker cost so the user can spot
            // unusually expensive runs without leaving the TUI.
            if let Some(cost) = a.cost_usd {
                items.push(kv_item(
                    "Cost",
                    &format_cost_usd(cost),
                    Some(Color::rgb(180, 180, 180)),
                ));
            }
            // #272-followup: surface the local DB's review_verdict on
            // review-typed assignments so the user can see WHY a
            // merge is blocked without leaving the TUI.  Earlier
            // session feedback: "I cant access the review text and
            // tell if it passed or failed".
            if a.assignment_type.as_deref() == Some("review") {
                if let Some(verdict) = a.review_verdict.as_deref() {
                    let (label, color) = match verdict {
                        "approve" => ("✓ approved", Color::rgb(120, 200, 120)),
                        "request-changes" => (
                            "✗ changes requested",
                            Color::rgb(220, 100, 100),
                        ),
                        other => (other, Color::rgb(220, 180, 100)),
                    };
                    items.push(kv_item("Verdict", label, Some(color)));
                } else {
                    items.push(kv_item(
                        "Verdict",
                        "(pending — not parsed yet)",
                        Some(Color::rgb(160, 160, 180)),
                    ));
                }
            }
            items.push(kv_item("", "", None));
        }
    }

    /// Push detail rows for the merge stage from `merge_queue`.
    fn append_merge_stage_rows(&self, items: &mut Vec<ListItem>, issue: &PipelineIssue) {
        let entries: Vec<&MergeQueueEntry> = self
            .data
            .merge_queue
            .iter()
            .filter(|m| m.issue_number == Some(issue.number))
            .collect();
        if entries.is_empty() {
            items.push(kv_item("", "    (not queued)", Some(Color::rgb(140, 140, 140))));
            return;
        }
        for e in entries {
            let id_short = if e.assignment_id.len() > 8 { &e.assignment_id[..8] } else { &e.assignment_id };
            items.push(kv_item("Assignment", id_short, Some(Color::rgb(160, 200, 220))));
            let state_color = match e.state.as_str() {
                "merged" => Color::rgb(120, 200, 120),
                "failed" | "human_required" => Color::rgb(220, 70, 70),
                "open" | "queued" => Color::rgb(220, 180, 100),
                _ => Color::rgb(180, 180, 180),
            };
            items.push(kv_item("State", &e.state, Some(state_color)));
            // #241: if a conflict-fix is in flight or the entry is now
            // human_required, surface a one-line substate row so the user
            // doesn't have to guess what's happening.
            if let Some(cf) = self.data.assignments.iter().find(|a| {
                a.issue_number == issue.number
                    && a.assignment_type.as_deref() == Some("conflict-fix")
                    && (a.status == "running" || a.status == "pending")
            }) {
                items.push(kv_item(
                    "Conflict-fix",
                    &format!("Fixing on {} (assignment {})", cf.machine, cf.id),
                    Some(Color::rgb(220, 180, 100)),
                ));
            } else if e.state == "human_required" {
                items.push(kv_item(
                    "Conflict-fix",
                    "auto-fix did not resolve — manual rebase required",
                    Some(Color::rgb(220, 100, 100)),
                ));
            }
            if let Some(pr) = e.pr_number {
                items.push(kv_item("PR", &format!("#{}", pr), Some(Color::rgb(160, 200, 220))));
            }
            if let Some(url) = &e.pr_url {
                items.push(kv_item("URL", url, Some(Color::rgb(140, 170, 210))));
            }
            // #240: surface CI check status under the Merge stage when a PR
            // exists.  Loading state is implicit — the row only renders once
            // the background fetch returns.
            if let Some(pr) = e.pr_number {
                if let Some(summary) = self
                    .pipeline_ci_checks
                    .get(&(e.repo_github.clone(), pr))
                {
                    let terse = summary.terse();
                    let line = if summary.failed > 0 {
                        let names = summary.failed_names.join(", ");
                        let url = summary.first_failed_url.as_deref().unwrap_or("");
                        if url.is_empty() {
                            format!("{} — {} failed", terse, names)
                        } else {
                            format!("{} — {} failed ({})", terse, names, url)
                        }
                    } else if summary.running > 0 {
                        format!("{} — checks still running", terse)
                    } else if !terse.is_empty() {
                        terse
                    } else {
                        "no checks".to_string()
                    };
                    let color = if summary.failed > 0 {
                        Color::rgb(220, 100, 100)
                    } else if summary.running > 0 {
                        Color::rgb(220, 180, 100)
                    } else {
                        Color::rgb(120, 200, 120)
                    };
                    items.push(kv_item("Checks", &line, Some(color)));
                }
            }
        }
    }


    // ── Mouse dispatch ────────────────────────────────────────────────────

    /// Dispatch one mouse event. Called from `handle()` before the keyboard
    /// match so we can still pass `&UiEvent` to `board_tree.handle()`.
    /// Returns `true` if a redraw is needed.
    ///
    /// Uses [`ShellContext::in_sidebar`] / [`ShellContext::in_main`] to
    /// route between the sidebar list and the main detail panel.
    fn handle_mouse(
        &mut self,
        event: &UiEvent,
        backend: &mut dyn Backend,
        ctx: &ShellContext,
    ) -> bool {
        match event {
            UiEvent::MouseDown {
                position,
                button: MouseButton::Left,
                ..
            } => {
                let pos = *position;
                let lh = backend.line_height();
                // #259: an open context menu intercepts all clicks first
                // — outside the menu → dismiss; on an item → activate;
                // anywhere else inside the menu → swallow (keep open).
                if let Some(handled) = self.handle_context_menu_click(pos) {
                    return handled;
                }
                if ctx.in_sidebar(pos.x, pos.y) {
                    if let Some(sidebar_b) = ctx.sidebar_bounds() {
                        return self.mouse_sidebar_click(event, pos, sidebar_b, backend);
                    }
                    false
                } else if ctx.in_main(pos.x, pos.y) {
                    self.mouse_main_click(pos, ctx.main_bounds(), lh)
                } else {
                    false
                }
            }
            UiEvent::MouseDown {
                position,
                button: MouseButton::Right,
                ..
            } => {
                // #259: right-click opens a context menu for the row
                // under the cursor (Board / Pipeline sidebar only for
                // MVP).  We synthesise a left-click first so the row
                // gets focused / selected, then open the menu using the
                // newly-selected row as the target.
                let pos = *position;
                if ctx.in_sidebar(pos.x, pos.y) {
                    if let Some(sidebar_b) = ctx.sidebar_bounds() {
                        // Pre-select the row under the cursor by routing
                        // a left-click; existing handlers already update
                        // selection state and re-rebuild the sidebar.
                        let synthetic_left = UiEvent::MouseDown {
                            widget: None,
                            button: MouseButton::Left,
                            position: pos,
                            modifiers: quadraui::Modifiers::default(),
                        };
                        self.mouse_sidebar_click(&synthetic_left, pos, sidebar_b, backend);
                    }
                    let target = match self.active_view {
                        SidebarView::Board => {
                            let selected = self.board_selected_issue();
                            let (repo_name, issue_number) = match selected {
                                Some((r, n)) => (Some(r), Some(n)),
                                None => (self.board_active_repo().map(|s| s.to_string()), None),
                            };
                            // #260: classify the row at right-click time
                            // so the menu items reflect the current
                            // lifecycle state (Backlog → Refine, etc.).
                            let lifecycle = match (&repo_name, issue_number) {
                                (Some(r), Some(n)) => self.board_row_lifecycle(r, n),
                                _ => BoardRowLifecycle::Unknown,
                            };
                            Some(ContextMenuTarget::BoardRow {
                                issue_number,
                                repo_name,
                                lifecycle,
                            })
                        }
                        SidebarView::Pipeline => {
                            let sel = self
                                .pipeline_sel
                                .and_then(|i| self.pipeline_issues.get(i));
                            let issue_number = sel.map(|i| i.number);
                            // #262: classify the row at right-click time
                            // so the menu offers Start only when New.
                            let lifecycle = sel
                                .map(|i| self.pipeline_row_lifecycle(i))
                                .unwrap_or(PipelineRowLifecycle::Other);
                            Some(ContextMenuTarget::PipelineRow {
                                issue_number,
                                lifecycle,
                            })
                        }
                        _ => None,
                    };
                    if let Some(target) = target {
                        if self.open_context_menu(pos, target) {
                            return true;
                        }
                    }
                }
                false
            }

            UiEvent::Scroll { position, delta, .. } => {
                let pos = *position;
                let d = *delta;
                let lh = backend.line_height();
                if ctx.in_sidebar(pos.x, pos.y) {
                    if let Some(sidebar_b) = ctx.sidebar_bounds() {
                        return self.mouse_sidebar_scroll(event, d, sidebar_b, backend, lh);
                    }
                    false
                } else if ctx.in_main(pos.x, pos.y) {
                    self.mouse_main_scroll(d, ctx.main_bounds(), lh)
                } else {
                    false
                }
            }

            // #272: drive ToolbarHoverTracker from MouseMoved so the
            // hovered toolbar button gets a background tint without the
            // host having to track button bounds across frames.  A
            // change in the hovered id triggers a redraw.
            UiEvent::MouseMoved { position, .. } => {
                let pos = *position;
                let lh = backend.line_height();
                let mut redraw = false;
                if ctx.in_sidebar(pos.x, pos.y) {
                    if let Some(sidebar_b) = ctx.sidebar_bounds() {
                        let panel = self.build_sidebar_action_panel(lh);
                        let layout = panel.layout(
                            sidebar_b,
                            quadraui::SidebarPanelMeasure::new(lh, 8.0),
                            toolbar_tui_measure,
                        );
                        if let Some(t) = layout.toolbar_layout.as_ref() {
                            redraw |= self
                                .sidebar_action_bar_hover
                                .update(t, pos.x, pos.y);
                        } else {
                            redraw |= self.sidebar_action_bar_hover.clear();
                        }
                        redraw |= self.panel_toolbar_hover.clear();
                    }
                } else if ctx.in_main(pos.x, pos.y) {
                    if let Some(toolbar) = self.panel_toolbar() {
                        let panel = SidebarPanel {
                            id: WidgetId::new("panel-toolbar"),
                            toolbar: Some(toolbar),
                            toolbar_height: Some(self.toolbar_height(lh)),
                        };
                        let layout = panel.layout(
                            ctx.main_bounds(),
                            quadraui::SidebarPanelMeasure::new(lh, 8.0),
                            toolbar_tui_measure,
                        );
                        if let Some(t) = layout.toolbar_layout.as_ref() {
                            redraw |= self.panel_toolbar_hover.update(t, pos.x, pos.y);
                        } else {
                            redraw |= self.panel_toolbar_hover.clear();
                        }
                    } else {
                        redraw |= self.panel_toolbar_hover.clear();
                    }
                    redraw |= self.sidebar_action_bar_hover.clear();
                } else {
                    redraw |= self.sidebar_action_bar_hover.clear();
                    redraw |= self.panel_toolbar_hover.clear();
                }
                redraw
            }

            _ => false,
        }
    }

    /// Click in the sidebar (board sidebar system / machines list,
    /// depending on the active view).
    fn mouse_sidebar_click(
        &mut self,
        event: &UiEvent,
        pos: Point,
        sidebar_b: Rect,
        backend: &mut dyn Backend,
    ) -> bool {
        // #270: action bar (row of contextual verb buttons) sits above
        // the tree.  Hit-test it first; if the click landed on a button
        // we've already dispatched the action.  Pass the shrunken rect
        // to the tree's hit-tester so its math doesn't see the bar row.
        let lh = backend.line_height();
        let (sidebar_b, consumed) =
            self.hit_test_sidebar_action_bar(pos, sidebar_b, lh);
        let _ = backend; // backend reserved for hover updates wired below
        if consumed {
            return true;
        }
        match self.active_view {
            SidebarView::Board => {
                let result = self.board_sidebar.handle(event, backend, sidebar_b);
                match result {
                    SidebarEvent::RowSelected { section, ref path } => {
                        if path.len() == 1 {
                            // Single-click on a status-group header toggles expansion.
                            let offset = self.board_repo_offset();
                            if section >= offset {
                                let repo_idx = section - offset;
                                if let Some(repo) = self.board_repo_names.get(repo_idx).cloned() {
                                    let group_idx = path[0] as usize;
                                    let cache = self.board_issues_cache.clone();
                                    let groups = self.board_grouped_for_repo(&cache, &repo);
                                    if let Some((key, _)) = groups.get(group_idx) {
                                        let key = key.to_string();
                                        let entry = self.board_status_expanded
                                            .entry((repo, key))
                                            .or_insert(true);
                                        *entry = !*entry;
                                        self.rebuild_board_sidebar();
                                    }
                                }
                            }
                        } else {
                            self.detail_scroll = 0;
                        }
                        true
                    }
                    SidebarEvent::RowActivated { section, ref path } => {
                        if path.len() == 1 {
                            let offset = self.board_repo_offset();
                            if section >= offset {
                                let repo_idx = section - offset;
                                if let Some(repo) = self.board_repo_names.get(repo_idx).cloned() {
                                    let group_idx = path[0] as usize;
                                    let cache = self.board_issues_cache.clone();
                                    let groups = self.board_grouped_for_repo(&cache, &repo);
                                    if let Some((key, _)) = groups.get(group_idx) {
                                        let key = key.to_string();
                                        let entry = self.board_status_expanded
                                            .entry((repo, key))
                                            .or_insert(true);
                                        *entry = !*entry;
                                        self.rebuild_board_sidebar();
                                    }
                                }
                            }
                        } else {
                            self.detail_scroll = 0;
                        }
                        true
                    }
                    SidebarEvent::HeaderActivated { section: _ } => true,
                    SidebarEvent::FormEvent { section: 0, event: FormEvent::TextInputChanged { ref value, .. } } => {
                        self.board_search = value.clone();
                        self.board_search_cursor = value.len();
                        self.rebuild_board_sidebar();
                        true
                    }
                    SidebarEvent::StateChanged
                    | SidebarEvent::Consumed
                    | SidebarEvent::ScrollChanged { .. }
                    | SidebarEvent::FormEvent { .. } => true,
                    _ => false,
                }
            }
            SidebarView::Machines => {
                // Row 0 = title strip; item i starts at row 1+i-scroll.
                let row = (pos.y - sidebar_b.y).max(0.0) as usize;
                if row >= 1 {
                    let item_idx = (row - 1) + self.machine_scroll;
                    let m = self.data.machines.len();
                    if item_idx < m && item_idx != self.machine_sel {
                        self.machine_sel = item_idx;
                        self.machine_detail_scroll = 0;
                        return true;
                    }
                }
                false
            }
            SidebarView::Pipeline => {
                let result = self.pipeline_sidebar.handle(event, backend, sidebar_b);
                self.pipeline_sel = self.selected_pipeline_index();
                match result {
                    SidebarEvent::RowSelected { .. }
                    | SidebarEvent::RowActivated { .. }
                    | SidebarEvent::HeaderActivated { .. }
                    | SidebarEvent::StateChanged
                    | SidebarEvent::Consumed
                    | SidebarEvent::ScrollChanged { .. } => true,
                    _ => false,
                }
            }
            SidebarView::Settings => {
                // #237: the Settings sidebar is now an empty placeholder —
                // all settings live in the main-panel form.  Clicks land in
                // the sidebar slot do nothing.
                let _ = (pos, sidebar_b, backend);
                false
            }
        }
    }

    /// Handle a left-click in the main panel.
    ///
    /// Routes the click to the right handler depending on the active view:
    /// - **Settings** — delegates to the `FormController` (field clicks, toggle, segmented control).
    /// - **Board** — handles the Board/Issue tab bar.
    /// - **Pipeline** — handles the tab bar and the `PipelineView` primitive
    ///   hit-test (dispatches the Go action when a stage button is clicked).
    /// - **Machines** — no-op (no interactive elements in the main panel).
    fn mouse_main_click(&mut self, pos: Point, main_b: Rect, lh: f32) -> bool {
        // #249 Principle 1: toolbar row at the top of main_content_bounds
        // is hit-tested first.  A click inside it dispatches the action
        // bound to the corresponding `toolbar:<verb>` segment.  We
        // shrink `main_b` for the rest of the handler so existing
        // tab-bar math (which expects (0..tab_h) from the panel's top)
        // continues to work unchanged.
        let (content_main_b, toolbar_consumed) =
            self.hit_test_panel_toolbar(pos, main_b, lh);
        if toolbar_consumed {
            return true;
        }
        let main_b = content_main_b;

        if self.active_view == SidebarView::Settings {
            // Route click to FormController. FormController::handle_cached
            // uses metrics cached by render_and_cache().
            use quadraui::Modifiers;
            let click_event = UiEvent::MouseDown {
                widget: None,
                button: MouseButton::Left,
                position: pos,
                modifiers: Modifiers::default(),
            };
            let result = self.settings_form.borrow_mut().handle_cached(&click_event, main_b);
            match result {
                FormControllerEvent::FormAction(ref form_event) => {
                    // Sync keyboard focus indicator to the clicked field so
                    // keyboard navigation picks up from where the mouse clicked.
                    let clicked_id = match form_event {
                        FormEvent::SegmentedControlChanged { id, .. }
                        | FormEvent::ToggleChanged { id, .. } => Some(id.clone()),
                        _ => None,
                    };
                    let changed = self.apply_settings_event(form_event);
                    if changed {
                        if let Some(id) = clicked_id {
                            let field_ids = self.settings_interactive_field_ids();
                            if let Some(pos) = field_ids.iter().position(|fid| fid == &id) {
                                self.settings_field_sel = pos;
                            }
                        }
                    }
                    return changed;
                }
                FormControllerEvent::ScrollChanged | FormControllerEvent::Consumed => return true,
                FormControllerEvent::Ignored => return false,
            }
        }
        if self.active_view == SidebarView::Board {
            // #269: hit-test from the actual TabBar labels (char widths)
            // instead of hard-coded offsets.  This stays correct when
            // tabs are renamed or have a badge appended.
            let tab_h = lh * 1.4;
            if pos.y - main_b.y < tab_h {
                let bar = self.board_detail_tab_bar();
                let labels: Vec<&str> = bar.tabs.iter().map(|t| t.label.as_str()).collect();
                if let Some(idx) = hit_tab_index_from_labels(&labels, main_b.x, pos.x) {
                    let new_tab = match idx {
                        0 => BoardDetailTab::Board,
                        _ => BoardDetailTab::Issue,
                    };
                    if new_tab != self.board_detail_tab {
                        self.board_detail_tab = new_tab;
                        self.detail_scroll = 0;
                        return true;
                    }
                }
                return false;
            }
            return false;
        }
        if self.active_view == SidebarView::Pipeline {
            // Tab bar occupies the first `lh * 1.4` row of the main panel.
            let tab_h = lh * 1.4;
            if pos.y - main_b.y < tab_h {
                let bar = self.pipeline_detail_tab_bar();
                let labels: Vec<&str> = bar.tabs.iter().map(|t| t.label.as_str()).collect();
                if let Some(idx) = hit_tab_index_from_labels(&labels, main_b.x, pos.x) {
                    let new_tab = match idx {
                        0 => PipelineDetailTab::Pipeline,
                        1 => PipelineDetailTab::Issue,
                        _ => PipelineDetailTab::Stages,
                    };
                    if new_tab != self.pipeline_detail_tab {
                        self.pipeline_detail_tab = new_tab;
                        self.pipeline_detail_scroll = 0;
                        return true;
                    }
                    return false;
                }
                return false;
            }
            // Below the tab row → the active tab's content. The PipelineView
            // is rendered into the content area (main_b minus tab row), so
            // we must hit-test against that rect — not main_b directly, or
            // the y-coordinates are off by tab_h.
            if self.pipeline_detail_tab == PipelineDetailTab::Pipeline {
                if let Some(view) = self.build_pipeline_widget() {
                    let content_rect = Rect::new(
                        main_b.x,
                        main_b.y + tab_h,
                        main_b.width,
                        (main_b.height - tab_h).max(0.0),
                    );
                    let pv_rect = pipeline_detail_pv_rect(content_rect, lh);
                    let layout = tui_pipeline_layout(&view, pv_rect);
                    match layout.hit_test(pos.x, pos.y) {
                        PipelineHit::Action(stage_idx) => {
                            self.dispatch_pipeline_stage(stage_idx);
                            return true;
                        }
                        PipelineHit::Body(stage_idx) => {
                            // Click on a stage box (outside the action
                            // button) — set focus so the content panel
                            // below switches to this stage's output.
                            self.pipeline_focused_stage = Some(stage_idx);
                            self.pipeline_stage_content_scroll = 0;
                            return true;
                        }
                        PipelineHit::Empty => return false,
                    }
                }
            }
            return false;
        }
        false
    }

    /// Scroll wheel in the sidebar.
    fn mouse_sidebar_scroll(
        &mut self,
        event: &UiEvent,
        delta: ScrollDelta,
        sidebar_b: Rect,
        backend: &mut dyn Backend,
        lh: f32,
    ) -> bool {
        match self.active_view {
            SidebarView::Board => {
                // Delegate to the SidebarSystem's built-in scroll handler.
                self.board_sidebar.handle(event, backend, sidebar_b);
                true
            }
            SidebarView::Machines => {
                let visible = content_visible_rows(sidebar_b, lh);
                let m = self.data.machines.len();
                if delta.y > 0.0 {
                    // Scroll up → show earlier items.
                    self.machine_scroll = self.machine_scroll.saturating_sub(1);
                } else if delta.y < 0.0 {
                    // Scroll down → show later items.
                    let max = m.saturating_sub(visible);
                    self.machine_scroll = (self.machine_scroll + 1).min(max);
                }
                true
            }
            SidebarView::Pipeline => {
                self.pipeline_sidebar.handle(event, backend, sidebar_b);
                true
            }
            SidebarView::Settings => {
                // #237: sidebar is an empty placeholder.  Forward the wheel
                // event to the main-panel form so the user can scroll the
                // settings list even when their cursor lingers on the left.
                let _ = (delta, sidebar_b, backend);
                false
            }
        }
    }

    /// Scroll wheel in the main panel (detail / machine detail).
    fn mouse_main_scroll(&mut self, delta: ScrollDelta, main_b: Rect, lh: f32) -> bool {
        let visible = content_visible_rows(main_b, lh);
        // Stash the live viewport size — `watch_log_list` uses this to compute
        // a stick-to-bottom offset that keeps the last line on screen.
        self.last_main_visible_rows.set(visible.max(1));
        // Watch overlay takes over the main panel; route scrollwheel to it
        // regardless of which view is active underneath.
        if self.watch.is_some() {
            // SSE log lines drive the count when present; fall back to the
            // remote-log cache when SSE isn't yet connected.
            let items = self
                .watch_sse
                .as_ref()
                .map(|s| s.lines.len())
                .unwrap_or_else(|| self.watch_log_list().items.len());
            let max = items.saturating_sub(visible.saturating_sub(1));
            if let Some(w) = self.watch.as_mut() {
                // Anchor the current scroll position: a usize::MAX sentinel
                // means stick-to-bottom; convert that to the explicit max
                // before applying the wheel delta so wheel-up actually moves.
                if w.scroll == usize::MAX {
                    w.scroll = max;
                }
                if delta.y > 0.0 {
                    // Wheel up → older lines.
                    w.scroll = w.scroll.saturating_sub(3);
                } else if delta.y < 0.0 {
                    // Wheel down → newer; once at the bottom, re-enable
                    // stick-to-bottom so future appends keep auto-scrolling.
                    let new_scroll = (w.scroll + 3).min(max);
                    w.scroll = if new_scroll >= max { usize::MAX } else { new_scroll };
                }
            }
            return true;
        }
        match self.active_view {
            SidebarView::Board => {
                // Use the active tab's actual list so the scroll max matches
                // what's rendered. Board tab → detail_list; Issue tab → body.
                let items = match self.board_detail_tab {
                    BoardDetailTab::Board => self.detail_list().items.len(),
                    BoardDetailTab::Issue => self.board_issue_body_list().items.len(),
                };
                let max = items.saturating_sub(visible.saturating_sub(1));
                if delta.y > 0.0 {
                    self.detail_scroll = self.detail_scroll.saturating_sub(1);
                } else if delta.y < 0.0 {
                    self.detail_scroll = (self.detail_scroll + 1).min(max);
                }
                true
            }
            SidebarView::Machines => {
                let items = self.machine_detail_list().items.len();
                let max = items.saturating_sub(visible.saturating_sub(1));
                if delta.y > 0.0 {
                    self.machine_detail_scroll = self.machine_detail_scroll.saturating_sub(1);
                } else if delta.y < 0.0 {
                    self.machine_detail_scroll = (self.machine_detail_scroll + 1).min(max);
                }
                true
            }
            SidebarView::Pipeline => {
                // Issue tab body is the scrollable region on the Pipeline view.
                // The Pipeline and Stages tabs render fixed-size widgets, so
                // scrollwheel on those is consumed but otherwise inert.
                if self.pipeline_detail_tab == PipelineDetailTab::Issue {
                    let items = self.pipeline_issue_body_list().items.len();
                    let max = items.saturating_sub(visible.saturating_sub(1));
                    if delta.y > 0.0 {
                        self.pipeline_detail_scroll =
                            self.pipeline_detail_scroll.saturating_sub(1);
                    } else if delta.y < 0.0 {
                        self.pipeline_detail_scroll =
                            (self.pipeline_detail_scroll + 1).min(max);
                    }
                }
                let _ = visible;
                true
            }
            SidebarView::Settings => {
                // Forward scroll to FormController (scrolls through form fields).
                let scroll_event = UiEvent::Scroll {
                    widget: None,
                    position: Point::new(0.0, 0.0),
                    delta,
                };
                self.settings_form.borrow_mut().handle_cached(&scroll_event, main_b);
                true
            }
        }
    }

    fn status_bar(&self) -> StatusBar {
        let view_label = self.active_view.label();
        // Show a loading indicator while a background fetch is in flight;
        // otherwise show seconds since the last completed refresh.
        let (refresh_text, refresh_fg, refresh_bold) = if self.pending_data.is_some() {
            (" ↻ loading… ".to_string(), Color::rgb(255, 220, 80), true)
        } else {
            (
                format!(" ↻ {}s ", self.refreshed_at.elapsed().as_secs()),
                Color::rgb(140, 140, 140),
                false,
            )
        };
        let mut left = vec![
            StatusBarSegment {
                text: " coord-tui ".to_string(),
                fg: Color::rgb(255, 255, 255),
                bg: Color::rgb(25, 70, 130),
                bold: true,
                action_id: None,
            },
            // #201: surface the view-switch keys in the active view label so
            // new users discover that 1/2/3 swaps Board/Machines/Pipeline.
            // Without this hint, the Pipeline panel's [Go]/[Retry] buttons
            // (now wired to mouse clicks) are unreachable for users who don't
            // know about the activity-bar key shortcuts.
            StatusBarSegment {
                text: format!(" {}  [1=Board 2=Machines 3=Pipeline 4=Settings] ", view_label),
                fg: Color::rgb(200, 220, 255),
                bg: Color::rgb(40, 60, 90),
                bold: false,
                action_id: None,
            },
            StatusBarSegment {
                text: refresh_text,
                fg: refresh_fg,
                bg: Color::rgb(30, 30, 40),
                bold: refresh_bold,
                action_id: None,
            },
        ];
        // Non-blocking warning if the last load failed.
        if let Some((err_msg, when)) = &self.fetch_error {
            if when.elapsed() < Duration::from_secs(10) {
                left.push(StatusBarSegment {
                    text: format!(" ⚠ {} ", err_msg),
                    fg: Color::rgb(255, 160, 60),
                    bg: Color::rgb(50, 30, 10),
                    bold: true,
                    action_id: None,
                });
            }
        }
        // #251: persistent warning when coordinator.yml was not located at
        // startup.  Previously surfaced at the top of the bottom COMMANDS
        // panel; the status bar carries it now that the panel is gone.
        if self.command_runner.config_path.is_none() {
            left.push(StatusBarSegment {
                text: " ⚠ coordinator.yml not found ".to_string(),
                fg: Color::rgb(255, 255, 255),
                bg: Color::rgb(140, 60, 30),
                bold: true,
                action_id: None,
            });
        }
        if let Some((label, elapsed)) = self.command_runner.running_info() {
            left.push(StatusBarSegment {
                text: format!(" {} ({:.0}s) ", label, elapsed.as_secs_f64()),
                fg: Color::rgb(255, 220, 100),
                bg: Color::rgb(60, 50, 20),
                bold: true,
                action_id: None,
            });
        } else if let Some((msg, when)) = &self.command_runner.message {
            if when.elapsed() < Duration::from_secs(8) {
                left.push(StatusBarSegment {
                    text: format!(" {} ", msg),
                    fg: Color::rgb(120, 200, 120),
                    bg: Color::rgb(20, 50, 20),
                    bold: false,
                    action_id: None,
                });
            }
        }
        let proposals = self.data.proposals.len();
        let hints = if let Some((_, ref buf)) = self.pending_test_fail {
            // #200: inline reason input takes precedence over the purge prompt
            // and normal hints.
            format!(" Test failure reason: {}_  Enter=submit  Esc=cancel ", buf)
        } else if let Some(ref repo) = self.pending_force_merge {
            // #245: force-merge confirm prompt — overrides every other hint
            // until the user picks y/Y (confirm) or any other key (cancel).
            // Scope is shown so the user knows whether this hits one repo or
            // the whole queue.
            let scope = if repo.is_empty() {
                " (whole queue)".to_string()
            } else {
                format!(" --repo {}", repo)
            };
            format!(
                " Force-merge{} despite failed CI checks? y=confirm  Esc=cancel ",
                scope
            )
        } else if let Some((a, i)) = self.pending_purge {
            // Confirm prompt overrides the normal hints while purge is pending.
            format!(
                " Purge {} assignment{} + {} closed issue{} older than {}d? y=confirm  Esc=cancel ",
                a, if a == 1 { "" } else { "s" },
                i, if i == 1 { "" } else { "s" },
                self.purge_days
            )
        } else if self.active_view == SidebarView::Pipeline && self.test_gate_actionable() {
            // #200: surface the Test gate keybinds when actionable for the
            // currently-selected pipeline issue.
            // #235: include B (Phase 1 build) when no build is already in
            // flight for this work id.
            if self.can_trigger_test_build() {
                " Test gate: B=build  P=pass  F=fail  S=skip  q=quit ".to_string()
            } else {
                " Test gate: P=pass  F=fail  S=skip  q=quit ".to_string()
            }
        } else if self.can_bounce_work_after_test_fail() {
            // #236: Test just failed — the user's next step is to fix the
            // code and re-dispatch Work. Make the affordance obvious.
            " Test failed — R=re-dispatch Work  q=quit ".to_string()
        } else if self.can_dispatch_review_after_test_done() {
            // #236: Test passed — Review can be dispatched immediately
            // instead of waiting for the reconcile auto-dispatch tick.
            " Test passed — R=dispatch review  q=quit ".to_string()
        } else if self.active_view == SidebarView::Pipeline
            && self
                .ci_summary_for_selected_issue()
                .map(|s| s.has_failures())
                .unwrap_or(false)
        {
            // #240: when the selected pipeline issue's PR has failed CI
            // checks, swap the default hint so the user can't merge without
            // seeing it. coord merge will refuse by default; --force-merge
            // bypasses.
            let summary = self.ci_summary_for_selected_issue().unwrap();
            let names = if summary.failed_names.len() > 2 {
                format!("{} +{}", summary.failed_names[..2].join(", "), summary.failed_names.len() - 2)
            } else {
                summary.failed_names.join(", ")
            };
            format!(" Checks failed: {}  m=merge anyway  q=quit ", names)
        } else if self.merge_blocked_on_review_for_selected_issue() {
            // #253: the selected merge entry has no approved review on the
            // board.  Surface the block so the user doesn't press lowercase
            // `m` and silently merge unreviewed code.  Capital `M` runs
            // `coord merge --skip-review` for the deliberate override.
            " Merge blocked: review not yet approved  R=dispatch review  M=merge anyway  q=quit ".to_string()
        } else if self.active_view == SidebarView::Pipeline {
            // #194: Pipeline-specific hints: refresh, navigate, go, dismiss done.
            " j/k=nav  Enter=go  R=refresh  D=dismiss-done  h/l=tabs  q=quit ".to_string()
        } else {
            // #192: `p` / `a` / `A` retired alongside the PROPOSALS
            // section.  Right-click → Send to Pipeline (#261) is the
            // canonical dispatch path now.
            let _ = proposals;
            " n=notify  m=merge  R=retry  P=purge  q=quit ".to_string()
        };
        StatusBar {
            id: WidgetId::new("statusbar"),
            left_segments: left,
            right_segments: vec![StatusBarSegment {
                text: hints,
                fg: Color::rgb(140, 140, 140),
                bg: Color::rgb(30, 30, 40),
                bold: false,
                action_id: None,
            }],
        }
    }
}

// ─── Right-click context menu (#259) ─────────────────────────────────────────

impl CoordApp {
    /// #260: classify a Board row by repo + issue number into a
    /// lifecycle bucket.  Drives which menu items appear at right-click
    /// time (Refine on Backlog rows, etc.).
    ///
    /// Cross-references both `data.open_issues` (for labels + state)
    /// and `data.assignments` (for "has assignments → In-flight").
    fn board_row_lifecycle(
        &self,
        repo_name: &str,
        issue_number: u64,
    ) -> BoardRowLifecycle {
        let issue = self
            .data
            .open_issues
            .iter()
            .find(|oi| oi.repo_name == repo_name && oi.number == issue_number);
        let has_assignment = self
            .data
            .assignments
            .iter()
            .any(|a| a.repo == repo_name && a.issue_number == issue_number);
        let Some(issue) = issue else {
            // No cache row — fall back on assignment presence; without
            // either signal we don't know the state.
            return if has_assignment {
                BoardRowLifecycle::InFlight
            } else {
                BoardRowLifecycle::Unknown
            };
        };
        if issue.state == "closed" && has_assignment {
            return BoardRowLifecycle::Completed;
        }
        if has_assignment {
            return BoardRowLifecycle::InFlight;
        }
        // Open issue with no assignments — classify by labels.
        let has_refining = issue.labels.iter().any(|l| l == "status:refining");
        let has_ready = issue.labels.iter().any(|l| l == "status:ready");
        if has_refining {
            BoardRowLifecycle::Refining
        } else if has_ready {
            BoardRowLifecycle::Refined
        } else {
            BoardRowLifecycle::Backlog
        }
    }

    /// Build the menu item list for a right-click on a Board sidebar row.
    ///
    /// Items vary by lifecycle bucket (#260 onward):
    /// - **Backlog** → Refine
    /// - other states → just Copy + Refresh for now (subsequent issues
    ///   wire #261 Send to Pipeline, #262 Start, etc.).
    fn context_menu_items_for_board_row(
        &self,
        issue_number: Option<u64>,
        lifecycle: &BoardRowLifecycle,
    ) -> Vec<ContextMenuItem> {
        let mut items: Vec<ContextMenuItem> = Vec::new();
        // #260: Refine is only meaningful for Backlog rows — adding
        // `status:refining` to a row that's already In-flight or
        // Completed would be confusing.
        if matches!(lifecycle, BoardRowLifecycle::Backlog) {
            items.push(ContextMenuItem::action("refine", "Refine"));
            items.push(ContextMenuItem::separator());
        }
        // #266: Refining rows get the forward path (Mark Refined) and
        // the backward path (Drop to Backlog).  Without these the
        // Refining state is a dead-end in the TUI — the user has to
        // shell out to `coord ready` to move forward.
        if matches!(lifecycle, BoardRowLifecycle::Refining) {
            items.push(ContextMenuItem::action("mark-refined", "Mark Refined"));
            items.push(ContextMenuItem::action("drop-to-backlog", "Drop to Backlog"));
            items.push(ContextMenuItem::separator());
        }
        // #261: Send to Pipeline is only meaningful for Refined rows
        // (scope locked, ready to dispatch).  Pre-Refined rows still
        // need scoping work; In-flight / Completed rows already have
        // the `coord` label.
        if matches!(lifecycle, BoardRowLifecycle::Refined) {
            items.push(ContextMenuItem::action(
                "send-to-pipeline",
                "Send to Pipeline",
            ));
            // #266: symmetric to Mark Refined — let the user walk back
            // to Refining if they want to re-open scoping.
            items.push(ContextMenuItem::action("drop-to-refining", "Drop to Refining"));
            items.push(ContextMenuItem::separator());
        }
        if let Some(num) = issue_number {
            items.push(ContextMenuItem::action(
                "copy-issue-number",
                &format!("Copy issue #{}", num),
            ));
            items.push(ContextMenuItem::separator());
        }
        items.push(
            ContextMenuItem::action("refresh", "Refresh").with_shortcut("r"),
        );
        items
    }

    /// #262: classify a Pipeline row into the umbrella's three sections
    /// plus an "Other" catch-all.  Drives the right-click menu so Start
    /// only appears when the row is genuinely dispatch-ready.
    ///
    /// Maps the existing 5-state `pipeline_lifecycle_section` to the
    /// 4-state lifecycle:
    /// - `"pending"` (coord + status:ready, no assignments) → **New**
    /// - `"in-progress"` (has assignments)                   → **InProgress**
    /// - `"done"`     (closed + assignments)                 → **Done**
    /// - `"refining"` / `"new"`                              → **Other**
    /// #271 part 2 follow-up: extract a PR number from a Pipeline
    /// issue.  Looks up the `merge_queue` entry — it's populated by
    /// `coord merge` when the PR is opened, and `pr_number` is on the
    /// entry directly.  Returns `None` when no PR has been opened yet
    /// (Plan- or Work-stage only, before a merge queue entry exists).
    fn pipeline_pr_number(&self, issue: &PipelineIssue) -> Option<i64> {
        self.data
            .merge_queue
            .iter()
            .find(|m| m.issue_number == Some(issue.number))
            .and_then(|m| m.pr_number)
    }

    /// #271 part 2 follow-up: cache-aware accessor for a PR's title /
    /// body / files-changed.  Returns `Some(cached)` on hit; on miss,
    /// kicks off a background `gh pr view` (only once per key — repeat
    /// calls coalesce on the in-flight `Receiver`) and returns None
    /// while the fetch is in flight.  Drained by
    /// `poll_pending_pr_fetches` each tick.
    fn pr_info_for_issue(&self, issue: &PipelineIssue) -> Option<FetchedPr> {
        let pr_number = self.pipeline_pr_number(issue)?;
        let key = (issue.repo_slug.clone(), pr_number);
        // Cache hit — done.
        if let Some(cached) = self.fetched_prs_cache.borrow().get(&key).cloned() {
            return Some(cached);
        }
        // Already fetching — let the poll loop pick it up.
        if self.pending_pr_fetches.borrow().contains_key(&key) {
            return None;
        }
        // Kick off a fresh background fetch.
        let rx = spawn_pr_fetch(issue.repo_slug.clone(), pr_number);
        self.pending_pr_fetches.borrow_mut().insert(key, rx);
        None
    }

    /// Drain completed `gh pr view` fetches into the in-memory cache.
    /// Returns `true` when at least one fetch resolved — caller should
    /// redraw so the Test guidance block picks up the new info.
    fn poll_pending_pr_fetches(&self) -> bool {
        use std::sync::mpsc::TryRecvError;
        let mut changed = false;
        let mut done: Vec<(String, i64)> = Vec::new();
        let pending = self.pending_pr_fetches.borrow();
        for (key, rx) in pending.iter() {
            match rx.try_recv() {
                Ok(Ok(_)) | Ok(Err(_)) | Err(TryRecvError::Disconnected) => {
                    done.push(key.clone());
                }
                Err(TryRecvError::Empty) => {}
            }
        }
        drop(pending);
        for key in done {
            // Pull the receiver back out and consume the message.
            let rx = match self.pending_pr_fetches.borrow_mut().remove(&key) {
                Some(rx) => rx,
                None => continue,
            };
            match rx.try_recv() {
                Ok(Ok(fp)) => {
                    self.fetched_prs_cache.borrow_mut().insert(key, fp);
                    changed = true;
                }
                Ok(Err(_)) | Err(_) => {
                    // Fetch failed — leave the cache untouched; the
                    // next render will trigger a retry.  Worth a
                    // background poll later (#TBD) instead of
                    // re-fetching on every render but the cost is
                    // ~200ms per failure so leave for now.
                }
            }
        }
        changed
    }

    fn pipeline_row_lifecycle(&self, issue: &PipelineIssue) -> PipelineRowLifecycle {
        match self.pipeline_lifecycle_section(issue) {
            "pending" => PipelineRowLifecycle::New,
            "in-progress" => PipelineRowLifecycle::InProgress,
            "done" => PipelineRowLifecycle::Done,
            _ => PipelineRowLifecycle::Other,
        }
    }

    /// Build the menu item list for a right-click on a Pipeline sidebar row.
    ///
    /// New rows (coord + status:ready, no assignments) get the two
    /// Start variants from #262 — Start with Plan (dispatches a Plan
    /// worker first, gated by `coord approve-plan`) and Skip Plan
    /// (dispatches Work directly).  Other states omit them.
    /// True when the row identified by `issue_number` has at least one
    /// review-typed assignment with `verdict='request-changes'`.  Drives
    /// the "Address review findings" menu / action-bar item — only
    /// shown when there's actually a review for the user to address.
    fn selected_row_has_request_changes_for(&self, issue_number: Option<u64>) -> bool {
        let Some(num) = issue_number else { return false; };
        // Match by issue number across both review and work assignments;
        // the link is via review_of_assignment_id.  Filter the review
        // pool by issue_number directly — cheaper than walking the
        // merge_queue too.
        let work_ids: Vec<&str> = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == num)
            .filter(|a| a.assignment_type.as_deref() == Some("work"))
            .filter_map(|a| Some(a.id.as_str()))
            .collect();
        self.data
            .assignments
            .iter()
            .filter(|a| a.assignment_type.as_deref() == Some("review"))
            .filter(|a| a.review_verdict.as_deref() == Some("request-changes"))
            .any(|a| {
                a.review_of_assignment_id
                    .as_deref()
                    .map(|id| work_ids.iter().any(|w| *w == id))
                    .unwrap_or(false)
            })
    }

    fn context_menu_items_for_pipeline_row(
        &self,
        issue_number: Option<u64>,
        lifecycle: &PipelineRowLifecycle,
    ) -> Vec<ContextMenuItem> {
        let mut items: Vec<ContextMenuItem> = Vec::new();
        match lifecycle {
            PipelineRowLifecycle::New => {
                items.push(ContextMenuItem::action(
                    "start-with-plan",
                    "Start with Plan",
                ));
                items.push(ContextMenuItem::action(
                    "start-skip-plan",
                    "Skip Plan, start Work",
                ));
                items.push(ContextMenuItem::separator());
            }
            PipelineRowLifecycle::InProgress => {
                // Watch is also reachable via Enter on the row; surfacing
                // it here gives the no-right-click / Android-over-SSH
                // path a clickable affordance.
                items.push(
                    ContextMenuItem::action("watch", "Watch")
                        .with_shortcut("Enter"),
                );
                items.push(ContextMenuItem::action("stop", "Stop"));
                // #bounce: when the latest review wants changes, offer to
                // dispatch a fix worker (auto-loop's path) right from
                // the row menu.  Shows up while the row is still
                // "in-progress" because the work assignment may have
                // completed but the issue's open state classifies it
                // as InProgress (assignments + issue open).
                if self.selected_row_has_request_changes_for(issue_number) {
                    items.push(
                        ContextMenuItem::action("bounce", "Address review findings")
                            .with_shortcut("f"),
                    );
                }
                items.push(ContextMenuItem::separator());
            }
            PipelineRowLifecycle::Done => {
                // Only meaningful when a PR exists (merge_queue entry
                // with a pr_number).  When no PR is open yet the
                // dispatcher toasts a "no PR" warning.
                items.push(ContextMenuItem::action("open-pr", "Open PR"));
                items.push(ContextMenuItem::separator());
            }
            PipelineRowLifecycle::Other => {}
        }
        if let Some(num) = issue_number {
            items.push(ContextMenuItem::action(
                "copy-issue-number",
                &format!("Copy issue #{}", num),
            ));
            items.push(ContextMenuItem::separator());
        }
        items.push(
            ContextMenuItem::action("refresh", "Refresh").with_shortcut("r"),
        );
        items
    }

    /// Open a right-click context menu anchored at `pos` for `target`.
    /// Pre-builds the item list and picks the first selectable item as the
    /// keyboard focus.  Returns `true` if a menu was opened (i.e. items
    /// are non-empty).
    fn open_context_menu(&mut self, pos: Point, target: ContextMenuTarget) -> bool {
        let items = match &target {
            ContextMenuTarget::BoardRow {
                issue_number,
                lifecycle,
                ..
            } => self.context_menu_items_for_board_row(*issue_number, lifecycle),
            ContextMenuTarget::PipelineRow {
                issue_number,
                lifecycle,
            } => self.context_menu_items_for_pipeline_row(*issue_number, lifecycle),
        };
        if items.is_empty() {
            return false;
        }
        // First non-separator item is the initial selection.
        let selected_idx = items
            .iter()
            .position(|it| it.action_id.is_some() && !it.disabled)
            .unwrap_or(0);
        self.pending_context_menu = Some(ContextMenuState {
            items,
            anchor: pos,
            selected_idx,
            target,
        });
        true
    }

    /// Render the open context menu (if any) on top of the rest of the UI.
    /// Caches the resolved layout so the click hit-test can match items
    /// without recomputing.  Pure render — no state mutation.
    fn render_context_menu(
        &self,
        backend: &mut dyn Backend,
        viewport: Rect,
    ) {
        let Some(state) = self.pending_context_menu.as_ref() else {
            return;
        };
        let menu = build_quadraui_context_menu(state);
        // Width = longest label + longest shortcut + padding (mirrors
        // vimcode's `render_impl.rs::draw_context_menu` heuristic).
        let max_label = state
            .items
            .iter()
            .map(|it| it.label.chars().count())
            .max()
            .unwrap_or(4);
        let max_shortcut = state
            .items
            .iter()
            .filter_map(|it| it.shortcut.as_ref())
            .map(|s| s.chars().count())
            .max()
            .unwrap_or(0);
        let menu_width = (max_label + max_shortcut + 6).clamp(20, 60) as f32;
        let lh = backend.line_height();
        let layout = menu.layout(
            state.anchor.x,
            state.anchor.y,
            viewport,
            menu_width,
            |_| ContextMenuItemMeasure::new(lh),
        );
        backend.draw_context_menu(&menu, &layout);
        *self.context_menu_layout.borrow_mut() = Some(layout);
    }

    /// Hit-test a click against the open context menu.  Returns
    /// `Some(true)` when the click landed on an actionable item (dispatched
    /// + menu dismissed), `Some(false)` when the click landed on a
    /// non-actionable cell of the menu (separator / disabled — swallow,
    /// keep menu open), or `None` when no menu is open.  Click outside the
    /// menu dismisses it and returns `Some(true)` to signal redraw.
    fn handle_context_menu_click(&mut self, pos: Point) -> Option<bool> {
        let layout = self.context_menu_layout.borrow().clone()?;
        if self.pending_context_menu.is_none() {
            return None;
        }
        match layout.hit_test(pos.x, pos.y) {
            ContextMenuHit::Item(id) => {
                let action_id = id.as_str().to_string();
                let state = self.pending_context_menu.take();
                *self.context_menu_layout.borrow_mut() = None;
                if let Some(state) = state {
                    self.dispatch_context_menu_action(&action_id, &state.target);
                }
                Some(true)
            }
            ContextMenuHit::Inert => Some(false),
            ContextMenuHit::Empty => {
                // Click outside dismisses without firing.
                self.pending_context_menu = None;
                *self.context_menu_layout.borrow_mut() = None;
                Some(true)
            }
        }
    }

    /// Move the keyboard selection within the open context menu, skipping
    /// separators and disabled items.  No-op when no menu is open.
    fn context_menu_move_selection(&mut self, delta: i32) {
        let Some(ref state) = self.pending_context_menu else {
            return;
        };
        // Build a transient quadraui menu just to reuse its move_selection
        // logic (skips separators + disabled items).
        let menu = build_quadraui_context_menu(state);
        let new_idx = menu.move_selection(state.selected_idx, delta);
        if let Some(ref mut s) = self.pending_context_menu {
            s.selected_idx = new_idx;
        }
    }

    /// Activate the keyboard-selected item (Enter) and dismiss the menu.
    /// No-op when no menu is open or the selected item is a separator.
    fn context_menu_activate_selected(&mut self) -> bool {
        let Some(state) = self.pending_context_menu.take() else {
            return false;
        };
        *self.context_menu_layout.borrow_mut() = None;
        let Some(item) = state.items.get(state.selected_idx) else {
            return false;
        };
        let Some(action_id) = item.action_id.clone() else {
            // Selected a separator — shouldn't happen since move_selection
            // skips them, but defend anyway.
            return false;
        };
        self.dispatch_context_menu_action(&action_id, &state.target);
        true
    }

    /// Dismiss the open context menu without firing an action.
    fn dismiss_context_menu(&mut self) {
        self.pending_context_menu = None;
        *self.context_menu_layout.borrow_mut() = None;
    }

    /// #266: shared helper for board-row context-menu actions that
    /// spawn a single `coord <subcommand> <repo> <issue>` command.
    /// `toast_title` and `body_template` (with `{}` for the issue
    /// number) drive the dispatch toast; on missing target data, the
    /// helper surfaces a guidance toast and returns `false` without
    /// spawning anything.
    fn dispatch_board_row_command(
        &mut self,
        target: &ContextMenuTarget,
        subcommand: &str,
        toast_title: &str,
        body_template: &str,
    ) -> bool {
        let (repo, num) = match target {
            ContextMenuTarget::BoardRow {
                issue_number: Some(num),
                repo_name: Some(repo),
                ..
            } => (repo.clone(), *num),
            _ => {
                self.push_toast(
                    &format!("{} unavailable", toast_title),
                    "No issue + repo target — focus a row first.",
                    ToastSeverity::Info,
                );
                return false;
            }
        };
        let num_str = num.to_string();
        if self.command_runner.spawn(&[subcommand, &repo, &num_str]) {
            let body = body_template.replace("{}", &num.to_string());
            self.push_toast(toast_title, &body, ToastSeverity::Info);
        } else {
            self.push_toast(
                "Another command is running",
                "Wait for the current command to finish, then try again.",
                ToastSeverity::Info,
            );
        }
        true
    }

    /// Route a context-menu `action_id` to the right behaviour.
    /// Stub actions for the MVP (#259); subsequent issues replace with
    /// row-state-specific dispatch.
    fn dispatch_context_menu_action(
        &mut self,
        action_id: &str,
        target: &ContextMenuTarget,
    ) -> bool {
        match action_id {
            "copy-issue-number" => {
                let num = match target {
                    ContextMenuTarget::BoardRow { issue_number, .. } => issue_number.unwrap_or(0),
                    ContextMenuTarget::PipelineRow { issue_number, .. } => {
                        issue_number.unwrap_or(0)
                    }
                };
                self.push_toast(
                    "Copy",
                    &format!(
                        "Copy of #{} not yet wired to the clipboard — primitive smoke test only.",
                        num,
                    ),
                    ToastSeverity::Info,
                );
                true
            }
            "refresh" => {
                self.refresh();
                true
            }
            // #260: Refine — move a Backlog row into the Refining
            // section by spawning `coord refine <repo> <num>`.
            "refine" => self.dispatch_board_row_command(
                target,
                "refine",
                "Refine",
                "#{}: tagging status:refining…",
            ),
            // #261: Send to Pipeline — add the `coord` label so the
            // issue moves Refined → Pipeline:New.  Wraps the new
            // `coord track <repo> <num>` Python command.
            "send-to-pipeline" => self.dispatch_board_row_command(
                target,
                "track",
                "Send to Pipeline",
                "#{} → Pipeline (adding coord label…)",
            ),
            // #266: Refining → Refined — wraps existing `coord ready`.
            "mark-refined" => self.dispatch_board_row_command(
                target,
                "ready",
                "Mark Refined",
                "#{} → Refined (tagging status:ready…)",
            ),
            // #266: Refining → Backlog (strips status:refining).
            // Refined → Refining is handled by `coord refine` which
            // also removes status:ready, so `drop-to-refining` reuses
            // the same command.
            "drop-to-backlog" => self.dispatch_board_row_command(
                target,
                "backlog",
                "Drop to Backlog",
                "#{}: stripping status:* label…",
            ),
            "drop-to-refining" => self.dispatch_board_row_command(
                target,
                "refine",
                "Drop to Refining",
                "#{}: tagging status:refining…",
            ),
            // #262: Start with Plan — dispatches a `type="plan"` worker
            // first.  Reuses the existing Pipeline-stage dispatcher so
            // the click + the [Go] button on the stage strip share one
            // code path.
            "start-with-plan" => {
                let dispatched = self.dispatch_pipeline_plan();
                if !dispatched {
                    self.push_toast(
                        "Start with Plan",
                        "Couldn't dispatch — see status bar for details.",
                        ToastSeverity::Warning,
                    );
                }
                true
            }
            // Pipeline:InProgress — Watch opens the live log overlay.
            // Same behaviour as Enter on a Pipeline row; the menu /
            // action-bar entry is for users without right-click or who
            // prefer mousing to keyboard shortcuts.
            "watch" => {
                let opened = self.open_watch_for_selected_issue();
                if !opened {
                    self.push_toast(
                        "Watch",
                        "No active assignment to watch for this issue.",
                        ToastSeverity::Warning,
                    );
                }
                true
            }
            // #bounce: address review findings — dispatches a fix
            // worker for the most recent review-typed assignment whose
            // verdict is `request-changes`.  Same code path as the auto-
            // loop's automatic bounce; this is the manual trigger for
            // when the auto-loop didn't fire (remote log unreachable,
            // notify happened too late, etc.) or for retrying.
            "bounce" => {
                self.dispatch_bounce_for_selected_pipeline_row();
                true
            }
            // Pipeline:InProgress — Stop cancels the running worker.
            // Mirrors `kill_watched` but works from outside the watch
            // overlay (which is the whole point — you shouldn't have
            // to open the overlay to kill a worker).
            "stop" => {
                let stopped = self.dispatch_stop_for_selected_pipeline_row();
                if !stopped {
                    self.push_toast(
                        "Stop",
                        "No running assignment to stop for this issue.",
                        ToastSeverity::Warning,
                    );
                }
                true
            }
            // Pipeline:Done — Open PR launches the PR in a browser via
            // `gh pr view --web`, which handles cross-platform browser
            // opening (xdg-open / open / start) on our behalf.
            "open-pr" => {
                let opened = self.dispatch_open_pr_for_selected_pipeline_row();
                if !opened {
                    self.push_toast(
                        "Open PR",
                        "No PR open yet for this issue.",
                        ToastSeverity::Warning,
                    );
                }
                true
            }
            // #262: Skip Plan, start Work — dispatches a `type="work"`
            // worker directly.  Same path the existing [Go] button on
            // the Work stage uses.
            "start-skip-plan" => {
                let dispatched = self.dispatch_pipeline_work();
                if !dispatched {
                    self.push_toast(
                        "Start Work",
                        "Couldn't dispatch — see status bar for details.",
                        ToastSeverity::Warning,
                    );
                }
                true
            }
            _ => {
                self.push_toast(
                    "Unknown context-menu action",
                    &format!("No handler for `{}` — likely a stale id.", action_id),
                    ToastSeverity::Warning,
                );
                false
            }
        }
    }
}

/// #269: Hit-test a click against a TUI tab bar's labels.  Walks the
/// labels left-to-right, accumulating character widths to derive each
/// tab's `start_x..end_x` boundary.  Returns the tab index under the
/// cursor or `None` if the click landed past the last tab.
///
/// Why not `Backend::tab_bar_layout`: that's the rasteriser's authoritative
/// hit-region map but requires a `&mut Backend` we don't want to plumb
/// through every test.  In the TUI rasteriser, labels are rendered 1:1
/// in character cells, so summing label char counts gives the exact
/// same boundaries the painter used.  GTK rendering uses pixel widths
/// and would need the layout call — track in a follow-up if that
/// backend ever has a regression here.
fn hit_tab_index_from_labels(labels: &[&str], origin_x: f32, click_x: f32) -> Option<usize> {
    let mut cursor = origin_x;
    for (i, label) in labels.iter().enumerate() {
        let width = label.chars().count() as f32;
        let end = cursor + width;
        if click_x >= cursor && click_x < end {
            return Some(i);
        }
        cursor = end;
    }
    None
}

/// Union of an optional rect and a required rect.  Used to compute the
/// context-menu viewport so a menu anchored in the sidebar can extend
/// rightward into the main panel without being clipped.
fn union_rects(a: Option<Rect>, b: Rect) -> Rect {
    let Some(a) = a else { return b; };
    let x = a.x.min(b.x);
    let y = a.y.min(b.y);
    let right = (a.x + a.width).max(b.x + b.width);
    let bottom = (a.y + a.height).max(b.y + b.height);
    Rect::new(x, y, right - x, bottom - y)
}

/// Convert an engine-side `ContextMenuState` into the `quadraui::ContextMenu`
/// primitive the rasteriser draws.  Separators map to `id: None`; actions
/// carry `action:<id>` so the hit-test can route back to the engine.
fn build_quadraui_context_menu(state: &ContextMenuState) -> ContextMenu {
    let items: Vec<QuiContextMenuItem> = state
        .items
        .iter()
        .map(|it| {
            if it.action_id.is_none() {
                // Separator.
                QuiContextMenuItem::default()
            } else {
                QuiContextMenuItem {
                    id: it.action_id.as_ref().map(WidgetId::new),
                    label: StyledText::plain(it.label.clone()),
                    detail: it
                        .shortcut
                        .as_ref()
                        .map(|s| StyledText::plain(s.clone())),
                    disabled: it.disabled,
                    ..Default::default()
                }
            }
        })
        .collect();
    ContextMenu {
        id: WidgetId::new("coord-context-menu"),
        items,
        selected_idx: state.selected_idx,
        bg: None,
        placement: ContextMenuPlacement::AnchorPoint,
    }
}

// ─── Selected-item action bar (#270) ─────────────────────────────────────────

impl CoordApp {
    /// #272: build the `SidebarPanel` for the Board / Pipeline sidebar.
    ///
    /// Always emits a header toolbar — even when no row-specific verbs
    /// apply — so the layout reserves the slot and the tree below
    /// doesn't shift between selections.  Returns the panel as a
    /// value rather than a `&SidebarPanel` because the toolbar buttons
    /// are derived from current state (selected row's lifecycle).
    fn build_sidebar_action_panel(&self, lh: f32) -> SidebarPanel {
        let toolbar = self.sidebar_action_bar().unwrap_or_else(|| Toolbar {
            id: WidgetId::new("sidebar-action-bar-empty"),
            buttons: Vec::new(),
            bg: None,
        });
        SidebarPanel {
            id: WidgetId::new("sidebar-panel"),
            toolbar: Some(toolbar),
            toolbar_height: Some(self.sidebar_action_bar_height(lh)),
        }
    }

    /// Height of the per-row action bar rendered above the sidebar tree.
    ///
    /// Two cells tall in TUI / two line-heights in GTK.  The quadraui
    /// rasteriser now paints multi-row toolbars (vertically centring
    /// the text) and `SidebarPanel` reserves the slot at the
    /// configured height even when the bar has no buttons — so this
    /// extra height is safe (no off-by-one click bug) and gives a
    /// proper button-row visual.
    fn sidebar_action_bar_height(&self, lh: f32) -> f32 {
        lh * 2.0
    }

    /// #270: build the contextual action bar for the currently-selected
    /// sidebar row.  `None` for views where the action bar doesn't apply
    /// (Machines / Settings) or when no row is selected.
    ///
    /// Mirrors the right-click context menu — same per-lifecycle item
    /// sets, same `action_id` strings — so dispatch routes through the
    /// same `dispatch_context_menu_action` code path the right-click
    /// already uses.  This is the no-mouse path for SSH / Termux / GTK
    /// users where right-click isn't available.
    fn sidebar_action_bar(&self) -> Option<Toolbar> {
        // Reuse the right-click target builder so the action set is the
        // same data the menu shows.  Returns None when no row is
        // selected — the caller renders nothing in that case.
        let target = self.target_for_selected_sidebar_row()?;
        let menu_items = match &target {
            ContextMenuTarget::BoardRow { issue_number, lifecycle, .. } => {
                self.context_menu_items_for_board_row(*issue_number, lifecycle)
            }
            ContextMenuTarget::PipelineRow { issue_number, lifecycle } => {
                self.context_menu_items_for_pipeline_row(*issue_number, lifecycle)
            }
        };
        // The menu always includes Copy + Refresh — those aren't
        // interesting in the action bar (Refresh has its own keybind,
        // Copy isn't wired to a clipboard yet).  Strip them so the bar
        // shows ONLY the row-state-specific verbs.
        let interesting: Vec<&ContextMenuItem> = menu_items
            .iter()
            .filter(|it| match it.action_id.as_deref() {
                Some("copy-issue-number") | Some("refresh") | None => false,
                _ => true,
            })
            .collect();
        if interesting.is_empty() {
            return None;
        }
        let buttons: Vec<ToolbarButton> = interesting
            .iter()
            .filter_map(|it| {
                // Items without an action_id are non-clickable headers
                // (none used by the row menus today) — skip them rather
                // than render an unclickable placeholder.
                let id_raw = it.action_id.as_ref()?;
                Some(ToolbarButton::Action {
                    id: WidgetId::new(format!("sidebar-action:{}", id_raw)),
                    label: it.label.clone(),
                    icon: icon_for_action(id_raw).map(String::from),
                    key_hint: it.shortcut.clone(),
                    // The right-click menu only surfaces actions when
                    // they apply to the current row state, so every
                    // item here is enabled.  When that changes (#272
                    // first-class wins), pipe per-item enabled state
                    // through ContextMenuItem instead.
                    enabled: true,
                    is_active: false,
                    tooltip: String::new(),
                })
            })
            .collect();
        if buttons.is_empty() {
            return None;
        }
        Some(Toolbar {
            id: WidgetId::new("sidebar-action-bar"),
            buttons,
            bg: None,
        })
    }

    /// Build a `ContextMenuTarget` for whichever sidebar row is currently
    /// selected.  Used by the action bar and (already) by the right-click
    /// path.  Returns `None` when no row is selected or the active view
    /// doesn't have row-level actions (Machines / Settings).
    fn target_for_selected_sidebar_row(&self) -> Option<ContextMenuTarget> {
        match self.active_view {
            SidebarView::Board => {
                let (repo, num) = self.board_selected_issue()?;
                let lifecycle = self.board_row_lifecycle(&repo, num);
                Some(ContextMenuTarget::BoardRow {
                    issue_number: Some(num),
                    repo_name: Some(repo),
                    lifecycle,
                })
            }
            SidebarView::Pipeline => {
                let issue = self
                    .pipeline_sel
                    .and_then(|i| self.pipeline_issues.get(i))?;
                let lifecycle = self.pipeline_row_lifecycle(issue);
                Some(ContextMenuTarget::PipelineRow {
                    issue_number: Some(issue.number),
                    lifecycle,
                })
            }
            _ => None,
        }
    }

    /// Hit-test a left-click against the sidebar action bar at the top
    /// of `sidebar_b`.  Returns `(shrunken_sidebar_b, consumed)` — same
    /// shape as `hit_test_panel_toolbar`.
    fn hit_test_sidebar_action_bar(
        &mut self,
        pos: Point,
        sidebar_b: Rect,
        lh: f32,
    ) -> (Rect, bool) {
        // #272: layout/hit-test goes through the SidebarPanel primitive
        // so click and paint can't drift apart.  `Content { .. }`
        // means the click landed below the toolbar slot (tree
        // territory) and the caller should forward to the tree.
        let panel = self.build_sidebar_action_panel(lh);
        let layout = panel.layout(
            sidebar_b,
            quadraui::SidebarPanelMeasure::new(lh, 8.0),
            toolbar_tui_measure,
        );
        let content_rect = layout.content_bounds;
        match layout.hit_test(pos.x, pos.y) {
            SidebarPanelHit::ToolbarButton(id) => {
                if let Some(action) = id.as_str().strip_prefix("sidebar-action:") {
                    let action = action.to_string();
                    if let Some(target) = self.target_for_selected_sidebar_row() {
                        self.dispatch_context_menu_action(&action, &target);
                    }
                }
                (content_rect, true)
            }
            // Click inside the toolbar slot but not on a clickable
            // button (gap, separator, or disabled) — swallow so it
            // doesn't fall through to the tree.
            SidebarPanelHit::ToolbarEmpty => (content_rect, true),
            // Below the toolbar slot — let the tree handle it.
            SidebarPanelHit::Content { .. } | SidebarPanelHit::Empty => (content_rect, false),
        }
    }
}

// ─── Per-panel toolbar (#249 Principle 1) ────────────────────────────────────

impl CoordApp {
    /// Height of the toolbar row at the top of the main panel.
    ///
    /// Two cells tall — matches `sidebar_action_bar_height` for visual
    /// consistency across the activity bar / sidebar / main panel
    /// chrome.  Safe to grow now that the quadraui rasteriser paints
    /// multi-row toolbars.
    fn toolbar_height(&self, lh: f32) -> f32 {
        lh * 2.0
    }

    /// Build the toolbar (a row of clickable verb buttons) for the current
    /// view, or `None` for views where no panel-level verbs apply.
    ///
    /// Backed by the quadraui [`Toolbar`] primitive — each button carries
    /// an `action_id` of the form `"toolbar:<verb>"` resolved by
    /// [`dispatch_toolbar_action`].  Disabled buttons set
    /// [`ToolbarButton::Action::enabled = false`] so the primitive dims
    /// them and the hit-test declines clicks; the affordance stays
    /// visible without misleading the user.
    fn panel_toolbar(&self) -> Option<Toolbar> {
        // Toolbar suppressed while the watch overlay or any inline confirm
        // prompt has the keyboard — these modes consume every keystroke
        // and a clickable toolbar above them would be misleading.
        if self.watch.is_some()
            || self.pending_purge.is_some()
            || self.pending_force_merge.is_some()
            || self.pending_test_fail.is_some()
        {
            return None;
        }

        // #192 / #263: toolbar revised — Plan and Approve dropped now
        // that Proposals is retired; Merge moves Pipeline-only.  Most
        // row-level actions live on the action bar (#270) or right-
        // click menu (#259-#262, #266); the panel toolbar is reserved
        // for genuine panel-wide ops.
        let buttons: Vec<ToolbarButton> = match self.active_view {
            SidebarView::Board => {
                vec![
                    toolbar_button("notify", "[N]otify", true),
                    toolbar_button(
                        "retry",
                        "[R]etry",
                        self.board_selected_failed_assignment().is_some(),
                    ),
                    toolbar_button(
                        "purge",
                        "[P]urge",
                        self.board_selection_in_completed_group(),
                    ),
                ]
            }
            SidebarView::Pipeline => {
                // Ready is meaningful when a pipeline issue is selected
                // and has a coord_repo mapping (otherwise `coord ready`
                // has nowhere to send the gh call).
                let ready_enabled = self
                    .pipeline_sel
                    .and_then(|i| self.pipeline_issues.get(i))
                    .map(|i| i.coord_repo.is_some())
                    .unwrap_or(false);
                // Retry on Pipeline view requires either an active stage
                // (Go/Retry button attached) or a Test-failed bounce.
                let retry_enabled = self.can_bounce_work_after_test_fail()
                    || self
                        .build_pipeline_widget()
                        .map(|w| w.stages.iter().any(|s| s.action.is_some()))
                        .unwrap_or(false);
                vec![
                    toolbar_button("notify", "[N]otify", true),
                    toolbar_button("ready", "[r]eady", ready_enabled),
                    // #272-followup: enable Merge only when the
                    // classifier says the selected issue is actually
                    // mergeable.  Disabled buttons render dimmed but
                    // keep their id (so hover tooltips can fire) and
                    // refuse clicks — better UX than silently
                    // dispatching a futile `coord merge`.
                    toolbar_button(
                        "merge",
                        "[M]erge",
                        matches!(
                            self.pipeline_merge_state(),
                            PipelineMergeState::Ready { .. }
                                | PipelineMergeState::BlockedOnCi { .. }
                        ),
                    ),
                    toolbar_button("retry", "[R]etry", retry_enabled),
                ]
            }
            // Machines and Settings have no panel-level verbs.
            SidebarView::Machines | SidebarView::Settings => return None,
        };

        Some(Toolbar {
            id: WidgetId::new("panel-toolbar"),
            buttons,
            bg: None,
        })
    }

    /// Hit-test a left-click against the panel toolbar at the top of
    /// the main content rect.  Returns `(shrunken_main_b, consumed)`:
    /// `consumed=true` means the click landed on a toolbar segment and
    /// the action was dispatched; the caller should NOT continue routing
    /// the click to the panel body.  `shrunken_main_b` is `main_b` with
    /// the toolbar row carved off the top, ready for downstream tab-bar
    /// hit-tests (whose math expects `pos.y - main_b.y < tab_h`).
    fn hit_test_panel_toolbar(
        &mut self,
        pos: Point,
        main_b: Rect,
        lh: f32,
    ) -> (Rect, bool) {
        let Some(toolbar) = self.panel_toolbar() else {
            return (main_b, false);
        };
        // #272: route through SidebarPanelLayout::hit_test so click +
        // paint share one definition of "where the toolbar slot is".
        let panel = SidebarPanel {
            id: WidgetId::new("panel-toolbar"),
            toolbar: Some(toolbar),
            toolbar_height: Some(self.toolbar_height(lh)),
        };
        let layout = panel.layout(
            main_b,
            quadraui::SidebarPanelMeasure::new(lh, 8.0),
            toolbar_tui_measure,
        );
        let content_rect = layout.content_bounds;
        match layout.hit_test(pos.x, pos.y) {
            SidebarPanelHit::ToolbarButton(id) => {
                let action_id = id.as_str().to_string();
                self.dispatch_toolbar_action(&action_id);
                (content_rect, true)
            }
            SidebarPanelHit::ToolbarEmpty => (content_rect, true),
            // Click landed below the toolbar — caller routes to the
            // tab bar / content beneath.
            SidebarPanelHit::Content { .. } | SidebarPanelHit::Empty => {
                (content_rect, false)
            }
        }
    }

    /// Resolve a toolbar `action_id` (e.g. `"toolbar:plan"`) to the same
    /// behaviour as the matching keybind.  Returns `true` if a redraw is
    /// required.  Mirrors the action paths in the key-press handler so
    /// click + keyboard stay in sync.
    fn dispatch_toolbar_action(&mut self, action_id: &str) -> bool {
        match action_id {
            // #192 / #263: `toolbar:plan` and `toolbar:approve` retired
            // alongside the PROPOSALS section.  The `coord plan` CLI
            // still works for scripts; the TUI just doesn't surface
            // it any more.
            "toolbar:notify" => {
                self.command_runner.spawn(&["notify"]);
                self.last_notify = Instant::now();
                true
            }
            "toolbar:merge" => {
                // #272-followup: shared classifier with the `m` keybind.
                // Outside the Pipeline view this falls through to plain
                // `coord merge` (server-side gates still apply).
                self.dispatch_pipeline_merge_for_selected_issue()
            }
            "toolbar:retry" => {
                if self.active_view == SidebarView::Pipeline {
                    if self.can_bounce_work_after_test_fail() {
                        self.dispatch_pipeline_work();
                    } else {
                        let dispatched = self.dispatch_pipeline_active_go();
                        if !dispatched {
                            self.push_toast(
                                "Nothing to retry",
                                "No pending or failed stage on the selected pipeline issue.",
                                ToastSeverity::Info,
                            );
                        }
                    }
                } else if let Some(a) = self.board_selected_failed_assignment() {
                    let id = a.id.clone();
                    self.command_runner.spawn(&["retry", &id]);
                } else {
                    self.push_toast(
                        "No failed assignment selected",
                        "Focus a row with status FAIL in the Board sidebar, then click Retry.",
                        ToastSeverity::Info,
                    );
                }
                true
            }
            "toolbar:purge" => {
                if self.active_view == SidebarView::Board
                    && self.board_selection_in_completed_group()
                {
                    let secs = self.purge_days as f64 * 86_400.0;
                    let counts = count_purgeable_db(secs).unwrap_or((0, 0));
                    self.pending_purge = Some(counts);
                } else {
                    self.push_toast(
                        "Purge only runs on the Completed group",
                        "Focus a done/merged row in the Board sidebar, then click Purge.",
                        ToastSeverity::Info,
                    );
                }
                true
            }
            "toolbar:ready" => {
                let selected = self
                    .pipeline_sel
                    .and_then(|i| self.pipeline_issues.get(i));
                match selected {
                    None => self.push_toast(
                        "Nothing to mark ready",
                        "Select an issue in the pipeline first.",
                        ToastSeverity::Info,
                    ),
                    Some(issue) if issue.coord_repo.is_none() => self.push_toast(
                        "No coord_repo mapping",
                        &format!(
                            "{} isn't mapped in coordinator.yml — add a `repos` entry first.",
                            issue.repo_slug,
                        ),
                        ToastSeverity::Warning,
                    ),
                    Some(issue) => {
                        let repo = issue.coord_repo.clone().unwrap();
                        let num_str = issue.number.to_string();
                        if self.command_runner.spawn(&["ready", &repo, &num_str]) {
                            self.pipeline_status = Some((
                                format!("#{}: marking ready", issue.number),
                                Instant::now(),
                            ));
                        }
                    }
                }
                true
            }
            _ => false,
        }
    }
}

/// Cell-width measure used to lay out a [`Toolbar`] for hit-testing.
///
/// Mirrors `quadraui::tui::toolbar::tui_item_width` exactly — that
/// helper is `pub(crate)` so we can't import it.  Keep in sync with
/// upstream when the rasteriser's framing changes (currently
/// `"[ icon? label (hint)? ]"` for actions, 2 cells for separators,
/// raw char width for labels).
fn toolbar_tui_measure(btn: &ToolbarButton) -> ToolbarItemMeasure {
    let w = match btn {
        ToolbarButton::Action {
            label,
            icon,
            key_hint,
            ..
        } => {
            let icon_w = icon.as_ref().map(|s| s.chars().count() + 1).unwrap_or(0);
            // " (xxx)" — 3 cells of decoration ("()" + leading space)
            // plus the hint's own char width.
            let hint_w = key_hint
                .as_ref()
                .map(|s| s.chars().count() + 3)
                .unwrap_or(0);
            // "[ " + content + " ]"
            (4 + icon_w + label.chars().count() + hint_w) as f32
        }
        ToolbarButton::Separator => 2.0,
        ToolbarButton::Label { text, .. } => text.chars().count() as f32,
    };
    ToolbarItemMeasure::new(w)
}

/// Helper that builds one `ToolbarButton::Action` for the panel toolbar.
/// Action id is always `toolbar:<verb>` — disabled buttons keep the id
/// (so the layout still records them for hover tooltips) but the
/// primitive's `enabled` flag prevents click dispatch.
fn toolbar_button(verb: &str, label: &str, enabled: bool) -> ToolbarButton {
    ToolbarButton::Action {
        id: WidgetId::new(format!("toolbar:{}", verb)),
        // Strip the surrounding spaces — the primitive adds its own
        // padding via `[ ... ]` framing in the TUI rasteriser.
        label: label.trim().to_string(),
        icon: icon_for_action(verb).map(String::from),
        key_hint: None,
        enabled,
        is_active: false,
        tooltip: String::new(),
    }
}

/// Map an `action_id` (sidebar row action or panel-toolbar verb) to a
/// short unicode glyph used as the button icon.  Plain printable
/// unicode rather than Private-Use-Area nerdfont so the icons render
/// on every terminal; the user can swap to nerdfont later if desired.
fn icon_for_action(action_id: &str) -> Option<&'static str> {
    match action_id {
        // Row actions (sidebar action bar).
        "refine" => Some("✎"),
        "mark-refined" => Some("✓"),
        "send-to-pipeline" => Some("→"),
        "drop-to-backlog" => Some("↩"),
        "drop-to-refining" => Some("↶"),
        "start-with-plan" => Some("☰"),
        "start-skip-plan" => Some("▶"),
        "watch" => Some("◉"),
        "stop" => Some("■"),
        "open-pr" => Some("↗"),
        "bounce" => Some("↺"),
        // Panel-level verbs (`toolbar:<verb>` keys after the prefix).
        "notify" => Some("ⓘ"),
        "retry" => Some("↻"),
        "purge" => Some("✕"),
        "ready" => Some("✓"),
        "merge" => Some("⤵"),
        _ => None,
    }
}

// ─── Shared periodic work (called from both handle() and tick()) ─────────────

impl CoordApp {
    /// Time-based housekeeping that must run regardless of whether a UI
    /// event arrived: toast pruning, data auto-refresh, background command
    /// runner draining, pipeline loader polling, and auto-notify when
    /// running assignments exist. Returns true if anything changed and a
    /// redraw is required.
    fn run_periodic_work(&mut self) -> bool {
        let mut needs_redraw = false;

        // Toast pruning
        let before = self.toasts.len();
        self.prune_toasts();
        if self.toasts.len() != before {
            needs_redraw = true;
        }

        // Auto-refresh: kick off background data load when interval elapses.
        // Uses the user-configured cadence from settings (default 5 s); when
        // the cadence is "Off" no automatic reload happens (manual `r` still works).
        let should_refresh = match self.settings.refresh_cadence.as_duration() {
            Some(cadence) => self.refreshed_at.elapsed() >= cadence,
            None => false,
        };
        if should_refresh && self.pending_data.is_none() {
            self.pending_data = Some(start_data_load());
            if self.active_view == SidebarView::Pipeline {
                self.maybe_kick_pipeline_loader();
            }
            needs_redraw = true;
        }

        // Poll background command runner
        if self.command_runner.poll() {
            self.refresh();
            needs_redraw = true;
        }

        // Poll background gh issue loader
        if self.poll_pipeline_loader() {
            needs_redraw = true;
        }

        // #240: keep merge-queue CI check summaries fresh on the Pipeline view.
        self.maybe_kick_ci_check_loaders();
        if self.poll_ci_check_loaders() {
            needs_redraw = true;
        }

        // Auto-notify: run `coord notify` when running assignments exist
        let has_running = self.data.assignments.iter().any(|a| a.status == "running");
        if has_running
            && self.last_notify.elapsed() >= NOTIFY_EVERY
            && !self.command_runner.is_running()
        {
            self.command_runner.spawn(&["notify"]);
            self.last_notify = Instant::now();
        }

        // Drain the SSE watch channel when the overlay is open. Any new lines
        // arriving from the background thread trigger a redraw so the UI
        // updates within one tick period without requiring user input.
        if self.watch_sse.is_some() {
            needs_redraw |= self.drain_sse_watch();
        }

        // #235: Drain Phase 1 build completions and toast the outcome.
        // Cheap no-op when no jobs are in flight.
        needs_redraw |= self.poll_test_build_jobs();

        // #271 part 2 follow-up: drain completed `gh pr view` fetches
        // into the cache so the Test guidance block picks them up.
        if self.poll_pending_pr_fetches() {
            needs_redraw = true;
        }

        needs_redraw
    }

    /// Drain pending messages from the SSE watch channel, accumulate lines,
    /// and handle reconnect/error logic.  Returns `true` when new data
    /// arrived and a redraw is needed.
    ///
    /// Reconnect strategy: on transient errors, reopen the stream using
    /// `Last-Event-Id` so replay starts from the last received byte offset.
    /// After **3 failures within 10 seconds**, a toast is shown and
    /// reconnection stops — the user must press `R` to retry manually.
    fn drain_sse_watch(&mut self) -> bool {
        let mut got_new = false;
        let mut needs_reconnect = false;
        let mut fail_limit_hit = false;

        // Drain all pending messages — borrow watch_sse mutably.
        if let Some(sse) = &mut self.watch_sse {
            if sse.done {
                return false;
            }
            loop {
                use std::sync::mpsc::TryRecvError;
                let msg = match sse.rx.try_recv() {
                    Ok(m) => m,
                    Err(TryRecvError::Empty) => break,
                    Err(TryRecvError::Disconnected) => {
                        // Background thread exited without sending; treat as error.
                        sse.fail_count += 1;
                        match sse.first_fail_at {
                            None => sse.first_fail_at = Some(Instant::now()),
                            Some(t) if t.elapsed() > Duration::from_secs(10) => {
                                sse.first_fail_at = Some(Instant::now());
                                sse.fail_count = 1;
                            }
                            _ => {}
                        }
                        if sse.fail_count >= 3 {
                            sse.done = true;
                            fail_limit_hit = true;
                        } else {
                            needs_reconnect = true;
                        }
                        got_new = true;
                        break;
                    }
                };

                match msg {
                    SseWatchMsg::Lines { last_id, text } => {
                        sse.last_event_id = last_id;
                        // Reassemble lines split across SSE chunks. The agent
                        // emits whatever it read from the log file (up to
                        // LOG_CHUNK_SIZE=4096 bytes), so a JSON line longer
                        // than that arrives in pieces. If the chunk doesn't
                        // end with `\n`, hold the trailing partial line until
                        // the next chunk completes it. Without this, broken
                        // half-lines reach parse_json_event and we lose
                        // fields like total_cost_usd / stop_reason that come
                        // after the split point.
                        let mut payload = std::mem::take(&mut sse.pending_tail);
                        payload.push_str(&text);
                        let (complete, tail) = if payload.ends_with('\n') {
                            (payload.clone(), String::new())
                        } else if let Some(last_nl) = payload.rfind('\n') {
                            let (a, b) = payload.split_at(last_nl + 1);
                            (a.to_string(), b.to_string())
                        } else {
                            (String::new(), payload.clone())
                        };
                        for line in complete.lines() {
                            sse.lines.push(line.to_string());
                        }
                        sse.pending_tail = tail;
                        got_new = true;
                    }
                    SseWatchMsg::Done { last_id } if !sse.pending_tail.is_empty() => {
                        // Stream is ending — flush any trailing partial line
                        // before transitioning to done. Without this, a final
                        // result line whose terminating `\n` never reached us
                        // (worker exited mid-write) would be invisible.
                        let tail = std::mem::take(&mut sse.pending_tail);
                        for line in tail.lines() {
                            sse.lines.push(line.to_string());
                        }
                        sse.last_event_id = last_id;
                        sse.done = true;
                        got_new = true;
                        break;
                    }
                    SseWatchMsg::Done { last_id } => {
                        sse.last_event_id = last_id;
                        sse.done = true;
                        got_new = true;
                        break;
                    }
                    SseWatchMsg::Error(msg) => {
                        // Surface the actual error in the log so the user
                        // can diagnose connection issues without grepping
                        // agent journalctl. Capped to one line in the panel.
                        sse.lines.push(format!("[sse error] {}", msg));
                        // Connection failure. Update the failure window.
                        sse.fail_count += 1;
                        match sse.first_fail_at {
                            None => sse.first_fail_at = Some(Instant::now()),
                            Some(t) if t.elapsed() > Duration::from_secs(10) => {
                                // Window expired: reset to a fresh 10-second window.
                                sse.first_fail_at = Some(Instant::now());
                                sse.fail_count = 1;
                            }
                            _ => {}
                        }
                        if sse.fail_count >= 3 {
                            sse.done = true;
                            fail_limit_hit = true;
                        } else {
                            needs_reconnect = true;
                        }
                        got_new = true;
                        break;
                    }
                    SseWatchMsg::Heartbeat => {
                        // No-op: the thread just confirmed the channel is alive.
                    }
                }
            }
        }

        // Post-drain: reconnect or show error toast (no watch_sse borrow held).
        if fail_limit_hit {
            self.push_toast(
                "SSE stream error",
                "Lost connection 3× in 10 s — press R to reconnect",
                ToastSeverity::Error,
            );
        } else if needs_reconnect {
            // Clone what we need before taking a new mutable borrow.
            let (host, assignment_id, last_id) = match &self.watch_sse {
                Some(s) => (s.host.clone(), s.assignment_id.clone(), s.last_event_id),
                None => return got_new,
            };
            let new_rx = spawn_sse_watch(&host, &assignment_id, last_id);
            if let Some(sse) = &mut self.watch_sse {
                sse.rx = new_rx;
            }
        }

        got_new
    }

    // ─── Settings panel ───────────────────────────────────────────────────────

    /// #237: empty sidebar list for the Settings view.  The form lives in
    /// the main panel and spans the full width; the sidebar shows just a
    /// header so the overall panel chrome stays consistent across views.
    fn settings_sidebar_placeholder(&self) -> ListView {
        ListView {
            id: WidgetId::new("settings-sidebar-placeholder"),
            title: Some(StyledText::plain(" SETTINGS ")),
            items: Vec::new(),
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: false,
            bordered: false,
        }
    }

    /// Build the unified settings `Form`.
    ///
    /// #237: All categories render as one full-width scrollable form with
    /// `FieldKind::Label` headers between groups — mirroring vimcode's
    /// settings layout.  The previous version split the panel into a
    /// category nav (sidebar) plus a per-category form (main); that split
    /// caused click hit-test misalignment because `FormController`'s cached
    /// metrics were sized for a different rect than where it was actually
    /// drawn.  One rect ⇒ no drift.
    ///
    /// Field IDs are stable across renders so the `FormController` can
    /// match events correctly.
    fn build_settings_form(&self) -> Form {
        let mut fields: Vec<FormField> = Vec::new();

        // ── Display ────────────────────────────────────────────────────
        fields.push(settings_label("Display"));
        fields.push(FormField {
            id: WidgetId::new("settings:theme"),
            label: StyledText::plain("Theme"),
            kind: FieldKind::SegmentedControl {
                options: Theme::LABELS.iter().map(|s| s.to_string()).collect(),
                selected_idx: self.settings.theme.to_idx(),
            },
            hint: StyledText::plain("Visual style (Light/High Contrast coming soon)"),
            disabled: false,
            validation: None,
        });

        // ── Refresh ────────────────────────────────────────────────────
        fields.push(settings_label("Auto-Refresh"));
        fields.push(FormField {
            id: WidgetId::new("settings:cadence"),
            label: StyledText::plain("Cadence"),
            kind: FieldKind::SegmentedControl {
                options: RefreshCadence::LABELS.iter().map(|s| s.to_string()).collect(),
                selected_idx: self.settings.refresh_cadence.to_idx(),
            },
            hint: StyledText::plain("How often the board is reloaded from the database"),
            disabled: false,
            validation: None,
        });

        // ── Notifications ──────────────────────────────────────────────
        fields.push(settings_label("Notifications"));
        fields.push(FormField {
            id: WidgetId::new("settings:audio"),
            label: StyledText::plain("Audio on completion"),
            kind: FieldKind::Toggle { value: self.settings.audio_on_completion },
            hint: StyledText::plain("Ring a bell when an assignment finishes"),
            disabled: false,
            validation: None,
        });

        // ── Watch Overlay ──────────────────────────────────────────────
        fields.push(settings_label("Watch Overlay"));
        fields.push(FormField {
            id: WidgetId::new("settings:log-ttl"),
            label: StyledText::plain("Log cache TTL"),
            kind: FieldKind::SegmentedControl {
                options: LogCacheTtl::LABELS.iter().map(|s| s.to_string()).collect(),
                selected_idx: self.settings.log_cache_ttl.to_idx(),
            },
            hint: StyledText::plain("How long a fetched log is reused before re-requesting"),
            disabled: false,
            validation: None,
        });

        // ── Machine Models ─────────────────────────────────────────────
        fields.push(settings_label("Per-Machine Model Overrides"));
        if self.data.machines.is_empty() {
            fields.push(FormField {
                id: WidgetId::new("settings:no-machines"),
                label: StyledText::plain("No machines available"),
                kind: FieldKind::ReadOnly {
                    value: StyledText::plain("—"),
                },
                hint: StyledText::plain("Machines are discovered from coordinator.yml"),
                disabled: true,
                validation: None,
            });
        }
        for machine in &self.data.machines {
            let current_pref = self
                .settings
                .machine_model
                .get(&machine.name)
                .copied()
                .unwrap_or_default();
            fields.push(FormField {
                id: WidgetId::new(format!("settings:model:{}", machine.name)),
                label: StyledText::plain(machine.name.clone()),
                kind: FieldKind::SegmentedControl {
                    options: ModelPref::LABELS.iter().map(|s| s.to_string()).collect(),
                    selected_idx: current_pref.to_idx(),
                },
                hint: StyledText::plain("Session-level override; coordinator.yml is the project default"),
                disabled: false,
                validation: None,
            });
        }

        // Compute focused_field from settings_field_sel, skipping label fields.
        let interactive: Vec<WidgetId> = fields
            .iter()
            .filter(|f| !matches!(f.kind, FieldKind::Label | FieldKind::ReadOnly { .. }))
            .map(|f| f.id.clone())
            .collect();
        let focused_field = interactive.get(self.settings_field_sel).cloned();

        Form {
            id: WidgetId::new("settings-form"),
            fields,
            focused_field,
            scroll_offset: self.settings_form.borrow().scroll_offset(),
            has_focus: true,
        }
    }

    /// Return the IDs of the interactive (non-label, non-read-only) fields
    /// for the current settings category, in form order.
    ///
    /// Used to map `settings_field_sel` to a concrete field when handling
    /// keyboard events.
    fn settings_interactive_field_ids(&self) -> Vec<WidgetId> {
        let form = self.build_settings_form();
        form.fields
            .iter()
            .filter(|f| !matches!(f.kind, FieldKind::Label | FieldKind::ReadOnly { .. }))
            .map(|f| f.id.clone())
            .collect()
    }

    /// Handle a directional key (h/l or Left/Right) against the focused
    /// settings form field.  Returns `true` when a setting changed.
    ///
    /// Builds the form only once to avoid the double-rebuild that occurred
    /// when `settings_interactive_field_ids` and the field-kind lookup both
    /// called `build_settings_form` separately.
    fn settings_change_focused(&mut self, direction: i32) -> bool {
        // Build once; extract both the interactive-field list and the kind.
        let form = self.build_settings_form();
        let interactive: Vec<_> = form
            .fields
            .iter()
            .filter(|f| !matches!(f.kind, FieldKind::Label | FieldKind::ReadOnly { .. }))
            .collect();
        let Some(field) = interactive.get(self.settings_field_sel) else {
            return false;
        };
        let field_id = field.id.clone();

        let event = match &field.kind {
            FieldKind::SegmentedControl { options, selected_idx } => {
                let n = options.len();
                if n == 0 {
                    return false;
                }
                let new_idx = if direction > 0 {
                    (selected_idx + 1) % n
                } else {
                    selected_idx.checked_sub(1).unwrap_or(n - 1)
                };
                FormEvent::SegmentedControlChanged {
                    id: field_id,
                    selected_idx: new_idx,
                }
            }
            FieldKind::Toggle { value } => {
                FormEvent::ToggleChanged {
                    id: field_id,
                    value: !value,
                }
            }
            _ => return false,
        };
        self.apply_settings_event(&event)
    }

    /// Apply a `FormEvent` from the settings form to the settings state,
    /// save to disk, and return `true` if something changed.
    ///
    /// When the save fails (e.g. read-only home directory), a non-fatal
    /// error toast is shown so the user is aware without interrupting their
    /// workflow.
    fn apply_settings_event(&mut self, event: &FormEvent) -> bool {
        match event {
            FormEvent::SegmentedControlChanged { id, selected_idx } => {
                match id.as_str() {
                    "settings:theme" => {
                        self.settings.theme = Theme::from_idx(*selected_idx);
                    }
                    "settings:cadence" => {
                        self.settings.refresh_cadence = RefreshCadence::from_idx(*selected_idx);
                    }
                    "settings:log-ttl" => {
                        self.settings.log_cache_ttl = LogCacheTtl::from_idx(*selected_idx);
                    }
                    field_id if field_id.starts_with("settings:model:") => {
                        let machine = field_id["settings:model:".len()..].to_string();
                        self.settings.machine_model.insert(machine, ModelPref::from_idx(*selected_idx));
                    }
                    _ => return false,
                }
                if let Err(e) = self.settings.save() {
                    self.push_toast("Settings", &format!("could not persist settings: {e}"), ToastSeverity::Error);
                }
                true
            }
            FormEvent::ToggleChanged { id, value } => {
                if id.as_str() == "settings:audio" {
                    self.settings.audio_on_completion = *value;
                    if let Err(e) = self.settings.save() {
                        self.push_toast("Settings", &format!("could not persist settings: {e}"), ToastSeverity::Error);
                    }
                    true
                } else {
                    false
                }
            }
            _ => false,
        }
    }
}

// ─── ShellApp implementation ──────────────────────────────────────────────────

impl ShellApp for CoordApp {
    /// Draw content into the shell's content zones.
    ///
    /// The shell has already rendered the activity bar, sidebar header, and
    /// divider. We draw:
    /// - The status bar into `layout.status_bar_bounds`.
    /// - The list (tree/machines/pipeline) into `layout.sidebar_content_bounds`.
    /// - The detail panel into `layout.main_content_bounds`.
    fn render_content(&self, backend: &mut dyn Backend, layout: &AppShellLayout) {
        let lh = backend.line_height();

        // ── Status bar ────────────────────────────────────────────────
        if let Some(sb_bounds) = layout.status_bar_bounds {
            backend.draw_status_bar(sb_bounds, &self.status_bar(), None, None);
        }

        // ── Sidebar: list content (sidebar system / machines) ────────
        if let Some(full_sidebar_rect) = layout.sidebar_content_bounds {
            // #270 / #272: SidebarPanel composes the contextual action
            // bar + the tree beneath into one primitive.  The slot is
            // always reserved at `sidebar_action_bar_height` even when
            // the bar has no buttons, so the tree doesn't shift as
            // selections cycle through lifecycle states; and click
            // dispatch routes through `SidebarPanelLayout::hit_test`
            // so the off-by-one math we had to maintain by hand is
            // gone.
            let panel = self.build_sidebar_action_panel(lh);
            let panel_layout = backend.draw_sidebar_panel(
                full_sidebar_rect,
                &panel,
                self.sidebar_action_bar_hover.hovered_id(),
                None,
            );
            let sidebar_rect = panel_layout.content_bounds;
            match self.active_view {
                SidebarView::Board => {
                    self.board_sidebar.render(backend, sidebar_rect);
                }
                SidebarView::Machines => {
                    backend.draw_list(sidebar_rect, &self.machines_list(true));
                }
                SidebarView::Pipeline => {
                    self.pipeline_sidebar.render(backend, sidebar_rect);
                }
                SidebarView::Settings => {
                    // #237: settings is one full-width form — no category
                    // nav.  Render an empty placeholder list so the sidebar
                    // slot keeps a header (consistent with other views)
                    // without offering a misleading affordance.
                    backend.draw_list(sidebar_rect, &self.settings_sidebar_placeholder());
                }
            }
        }

        // ── Main: detail panel only (full main_content_bounds) ───────
        let full_m = layout.main_content_bounds;
        // #249 Principle 1: draw the per-panel toolbar at the top of
        // main_content_bounds so every panel verb has a visible
        // affordance (Plan / Notify / Merge / etc.).  Carve the toolbar
        // row off the top; everything else renders into the shrunken
        // rect below it.
        let m = if let Some(toolbar) = self.panel_toolbar() {
            // #272: same SidebarPanel composition for the main-panel
            // toolbar so the tab bar below doesn't have to coordinate
            // its own slot carving.
            let panel = SidebarPanel {
                id: WidgetId::new("panel-toolbar"),
                toolbar: Some(toolbar),
                toolbar_height: Some(self.toolbar_height(lh)),
            };
            let panel_layout = backend.draw_sidebar_panel(
                full_m,
                &panel,
                self.panel_toolbar_hover.hovered_id(),
                None,
            );
            panel_layout.content_bounds
        } else {
            full_m
        };
        // Keep watch_log_list's stick-to-bottom math in sync with the live
        // viewport on every frame (not just when the user scrolls).
        self.last_main_visible_rows
            .set(content_visible_rows(m, lh).max(1));
        match self.active_view {
            SidebarView::Board => {
                // Tab bar (Board / Issue), then the active tab's content.
                let tab_bar = self.board_detail_tab_bar();
                let tab_h = lh * 1.4;
                let tab_rect = Rect::new(m.x, m.y, m.width, tab_h);
                let content_rect =
                    Rect::new(m.x, m.y + tab_h, m.width, (m.height - tab_h).max(0.0));
                backend.draw_tab_bar(tab_rect, &tab_bar, None);
                match self.board_detail_tab {
                    BoardDetailTab::Board => {
                        backend.draw_list(content_rect, &self.detail_list());
                    }
                    BoardDetailTab::Issue => {
                        backend.draw_list(content_rect, &self.board_issue_body_list());
                    }
                }
            }
            SidebarView::Machines => {
                backend.draw_list(m, &self.machine_detail_list());
            }
            SidebarView::Settings => {
                // Build form for the current category and render via
                // FormController (handles scrollbar + layout).
                let form = self.build_settings_form();
                let mut fc = self.settings_form.borrow_mut();
                fc.set_form(form);
                fc.render_and_cache(backend, m);
            }
            SidebarView::Pipeline => {
                // Watch overlay takes over the entire main panel when active —
                // tabs, pipeline view, meta line all hidden while watching.
                if self.watch.is_some() {
                    backend.draw_list(m, &self.watch_log_list());
                } else if self.pipeline_sel.is_none() && self.pipeline_issues.is_empty() {
                    backend.draw_list(m, &self.pipeline_placeholder_list());
                } else {
                    // Tab bar.
                    let tab_bar = self.pipeline_detail_tab_bar();
                    let tab_h = lh * 1.4;
                    let tab_rect = Rect::new(m.x, m.y, m.width, tab_h);
                    let content_rect = Rect::new(m.x, m.y + tab_h, m.width, (m.height - tab_h).max(0.0));
                    backend.draw_tab_bar(tab_rect, &tab_bar, None);

                    match self.pipeline_detail_tab {
                        PipelineDetailTab::Pipeline => {
                            let pv_rect = pipeline_detail_pv_rect(content_rect, lh);
                            let meta_rect = Rect::new(
                                content_rect.x,
                                pv_rect.y + pv_rect.height,
                                content_rect.width,
                                (content_rect.height - pv_rect.height).max(0.0),
                            );
                            if let Some(view) = self.build_pipeline_widget() {
                                backend.draw_pipeline_view(pv_rect, &view);
                            } else {
                                backend.draw_list(pv_rect, &self.pipeline_placeholder_list());
                            }
                            backend.draw_list(meta_rect, &self.pipeline_issue_summary());
                        }
                        PipelineDetailTab::Issue => {
                            backend.draw_list(content_rect, &self.pipeline_issue_body_list());
                        }
                        PipelineDetailTab::Stages => {
                            backend.draw_list(content_rect, &self.pipeline_stages_list());
                        }
                    }
                }
            }
        }

        // ── Toast overlay (bottom-right of main content) ────────────────
        if let Some(stack) = self.toast_stack() {
            backend.draw_toast_stack(layout.main_content_bounds, &stack);
        }

        // ── #259: open context menu (above everything) ──────────────────
        // Drawn last so it sits on top of toasts, the panel content, and
        // the toolbar.  The viewport unions the sidebar + main panel so a
        // menu anchored in the sidebar can flow rightward into the main
        // area without being clipped to the (narrow) sidebar width.
        if self.pending_context_menu.is_some() {
            let viewport = union_rects(
                layout.sidebar_content_bounds,
                layout.main_content_bounds,
            );
            self.render_context_menu(backend, viewport);
        } else {
            // Keep the cached layout in sync — clear it once the menu
            // is no longer rendered so a stale layout can't satisfy a
            // hit-test on the next click.
            *self.context_menu_layout.borrow_mut() = None;
        }
    }

    fn handle(&mut self, event: UiEvent, backend: &mut dyn Backend, ctx: &ShellContext) -> Reaction {
        let mut needs_redraw = false;

        // ── Drain pending background data load ──────────────────────────
        if self.apply_pending_data() {
            needs_redraw = true;
        }

        // ── Expire stale toasts ─────────────────────────────────────────
        needs_redraw |= self.run_periodic_work();

        // ── Mouse / scroll dispatch (before consuming the event) ─────────────
        needs_redraw |= self.handle_mouse(&event, backend, ctx);

        // ── Pre-compute panel bounds for keyboard visible-row estimates ───────
        let list_b = ctx.sidebar_bounds().unwrap_or(ctx.main_bounds());
        let lh = backend.line_height();

        // ── #200 Pending test-fail reason: intercept all keys until submit ────
        // Enter submits and records test_state=failed. Esc cancels. Backspace
        // edits. Any printable char appends. Same pattern as inject_focused.
        if self.pending_test_fail.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Named(NamedKey::Enter) => {
                        let reason = self
                            .pending_test_fail
                            .as_ref()
                            .map(|(_, b)| b.trim().to_string())
                            .unwrap_or_default();
                        let reason_opt = if reason.is_empty() { None } else { Some(reason.as_str()) };
                        self.record_test_verdict("failed", reason_opt);
                        self.pending_test_fail = None;
                    }
                    Key::Named(NamedKey::Escape) => {
                        self.pending_test_fail = None;
                    }
                    Key::Named(NamedKey::Backspace) => {
                        if let Some((_, ref mut buf)) = self.pending_test_fail {
                            buf.pop();
                        }
                    }
                    Key::Char(ch) => {
                        if let Some((_, ref mut buf)) = self.pending_test_fail {
                            buf.push(*ch);
                        }
                    }
                    _ => {}
                }
                return Reaction::Redraw;
            }
        }

        // ── Pending purge confirmation: intercept ALL key presses ─────────────
        // While a purge is pending, 'y'/'Y' executes it; every other key
        // cancels.  We return early so the normal key dispatch never fires.
        if self.pending_purge.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Char('y') | Key::Char('Y') => {
                        let secs = self.purge_days as f64 * 86_400.0;
                        match purge_done_assignments_db(secs) {
                            Ok((a, i)) => self.push_toast(
                                "Purge complete",
                                &format!("Removed {} assignment{} + {} closed issue{}",
                                    a, if a == 1 { "" } else { "s" },
                                    i, if i == 1 { "" } else { "s" }),
                                ToastSeverity::Info,
                            ),
                            Err(e) => self.push_toast(
                                "Purge failed",
                                &format!("{}", e),
                                ToastSeverity::Error,
                            ),
                        }
                        self.pending_purge = None;
                        self.refresh();
                    }
                    _ => {
                        // Any other key cancels — Escape, 'n', 'N', or anything else.
                        self.pending_purge = None;
                    }
                }
                return Reaction::Redraw;
            }
        }

        // ── #259: open context menu intercepts keyboard nav ──────────────
        // Up/Down/j/k move the keyboard selection (skipping separators);
        // Enter activates the selected item; Escape (or any other key)
        // dismisses without firing.
        if self.pending_context_menu.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Named(NamedKey::Down) | Key::Char('j') => {
                        self.context_menu_move_selection(1);
                    }
                    Key::Named(NamedKey::Up) | Key::Char('k') => {
                        self.context_menu_move_selection(-1);
                    }
                    Key::Named(NamedKey::Enter) => {
                        self.context_menu_activate_selected();
                    }
                    Key::Named(NamedKey::Escape) => {
                        self.dismiss_context_menu();
                    }
                    _ => {
                        // Any other key dismisses to keep the focus model
                        // simple — typing a global keybind while the menu
                        // is open shouldn't both dismiss and fire that
                        // bind, so we just dismiss.
                        self.dismiss_context_menu();
                    }
                }
                return Reaction::Redraw;
            }
        }

        // ── #245: Pending --force-merge confirmation: intercept ALL keys ──
        // The user has pressed `m` while the "Checks failed" hint was visible.
        // We refuse to bypass the CI gate without an explicit y/Y so a
        // fat-fingered `m` can't merge a red PR.
        if let Some(repo) = self.pending_force_merge.clone() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Char('y') | Key::Char('Y') => {
                        let scoped = !repo.is_empty();
                        let mut args: Vec<&str> = vec!["merge", "--force-merge"];
                        if scoped {
                            args.push("--repo");
                            args.push(&repo);
                        }
                        self.command_runner.spawn(&args);
                        let scope_str = if scoped {
                            format!(" --repo {}", repo)
                        } else {
                            String::new()
                        };
                        self.push_toast(
                            "Force-merge dispatched",
                            &format!("coord merge --force-merge{} — CI gate bypassed", scope_str),
                            ToastSeverity::Warning,
                        );
                        self.pending_force_merge = None;
                    }
                    _ => {
                        // Any other key cancels — Escape, 'n', 'N', anything.
                        self.pending_force_merge = None;
                        self.push_toast(
                            "Force-merge cancelled",
                            "CI gate stays in place",
                            ToastSeverity::Info,
                        );
                    }
                }
                return Reaction::Redraw;
            }
        }

        // ── Keyboard and window events ────────────────────────────────────────
        match &event {
            UiEvent::KeyPressed { key, .. } => {
                match key {
                    // ── Board search input ───────────────────────────────
                    // Escape clears search or (if already empty) quits.
                    Key::Named(NamedKey::Escape)
                        if self.active_view == SidebarView::Board
                            && !self.board_search.is_empty() =>
                    {
                        self.board_search.clear();
                        self.board_search_cursor = 0;
                        self.board_search_focused = false;
                        self.rebuild_board_sidebar();
                        needs_redraw = true;
                    }
                    // Backspace while search is active removes char before cursor.
                    Key::Named(NamedKey::Backspace)
                        if self.active_view == SidebarView::Board
                            && self.board_search_focused =>
                    {
                        if self.board_search_cursor > 0 {
                            // Find the start of the previous char (UTF-8 aware).
                            let mut prev = self.board_search_cursor - 1;
                            while prev > 0 && !self.board_search.is_char_boundary(prev) {
                                prev -= 1;
                            }
                            self.board_search.remove(prev);
                            self.board_search_cursor = prev;
                        }
                        self.rebuild_board_sidebar();
                        needs_redraw = true;
                    }
                    // Any printable char while search is active inserts at cursor.
                    Key::Char(ch)
                        if self.active_view == SidebarView::Board
                            && self.board_search_focused =>
                    {
                        self.board_search.insert(self.board_search_cursor, *ch);
                        self.board_search_cursor += ch.len_utf8();
                        self.rebuild_board_sidebar();
                        needs_redraw = true;
                    }
                    // '/' activates search when not already active.
                    Key::Char('/')
                        if self.active_view == SidebarView::Board
                            && !self.board_search_focused =>
                    {
                        self.board_search_focused = true;
                        self.rebuild_board_sidebar();
                        needs_redraw = true;
                    }

                    // ── Watch overlay: inject input mode takes priority ──
                    // When the inject prompt is open, ALL char/Enter/Esc/
                    // Backspace go to the input buffer until it closes.
                    Key::Named(NamedKey::Enter) if self.inject_focused => {
                        self.submit_inject();
                        needs_redraw = true;
                    }
                    Key::Named(NamedKey::Escape) if self.inject_focused => {
                        self.inject_input.clear();
                        self.inject_focused = false;
                        needs_redraw = true;
                    }
                    Key::Named(NamedKey::Backspace) if self.inject_focused => {
                        self.inject_input.pop();
                        needs_redraw = true;
                    }
                    Key::Char(ch) if self.inject_focused => {
                        self.inject_input.push(*ch);
                        needs_redraw = true;
                    }

                    // ── Watch overlay (no input active): control keys ────
                    Key::Char('b') if self.watch.is_some() && !self.inject_focused => {
                        self.inject_focused = true;
                        self.inject_input.clear();
                        needs_redraw = true;
                    }
                    Key::Char('K') if self.watch.is_some() && !self.inject_focused => {
                        self.kill_watched();
                        needs_redraw = true;
                    }
                    Key::Char('A') if self.watch.is_some() && !self.inject_focused => {
                        self.approve_watched_plan();
                        needs_redraw = true;
                    }
                    // R = force a fresh SSE connection from byte 0.
                    Key::Char('R') if self.watch.is_some() && !self.inject_focused => {
                        self.reset_sse_watch();
                        needs_redraw = true;
                    }
                    Key::Char('q') | Key::Named(NamedKey::Escape)
                        if self.watch.is_some() =>
                    {
                        self.close_watch();
                        needs_redraw = true;
                    }
                    Key::Char('j') | Key::Named(NamedKey::Down)
                        if self.watch.is_some() && !self.inject_focused =>
                    {
                        if let Some(w) = self.watch.as_mut() {
                            let current = if w.scroll == usize::MAX { 0 } else { w.scroll };
                            w.scroll = current.saturating_add(1);
                        }
                        needs_redraw = true;
                    }
                    Key::Char('k') | Key::Named(NamedKey::Up)
                        if self.watch.is_some() && !self.inject_focused =>
                    {
                        if let Some(w) = self.watch.as_mut() {
                            let current = if w.scroll == usize::MAX { 0 } else { w.scroll };
                            w.scroll = current.saturating_sub(1);
                        }
                        needs_redraw = true;
                    }

                    Key::Char('q') | Key::Named(NamedKey::Escape) => return Reaction::Exit,

                    // ── Switch sidebar views ─────────────────────────────
                    Key::Char('1') => {
                        self.active_view = SidebarView::Board;
                        needs_redraw = true;
                    }
                    Key::Char('2') => {
                        self.active_view = SidebarView::Machines;
                        needs_redraw = true;
                    }
                    Key::Char('3') => {
                        self.active_view = SidebarView::Pipeline;
                        self.maybe_kick_pipeline_loader();
                        needs_redraw = true;
                    }
                    Key::Char('4') => {
                        self.active_view = SidebarView::Settings;
                        needs_redraw = true;
                    }

                    // ── Settings panel keyboard nav ──────────────────────
                    // #237: j/k now step through the unified form's
                    // interactive fields directly — there are no
                    // categories to navigate any more.
                    Key::Char('j') | Key::Named(NamedKey::Down)
                        if self.active_view == SidebarView::Settings =>
                    {
                        let count = self.settings_interactive_field_ids().len();
                        if count > 0 {
                            self.settings_field_sel =
                                (self.settings_field_sel + 1).min(count - 1);
                        }
                        needs_redraw = true;
                    }
                    Key::Char('k') | Key::Named(NamedKey::Up)
                        if self.active_view == SidebarView::Settings =>
                    {
                        self.settings_field_sel = self.settings_field_sel.saturating_sub(1);
                        needs_redraw = true;
                    }
                    // Tab — next interactive field within the form
                    Key::Named(NamedKey::Tab)
                        if self.active_view == SidebarView::Settings =>
                    {
                        let count = self.settings_interactive_field_ids().len();
                        if count > 1 {
                            self.settings_field_sel = (self.settings_field_sel + 1) % count;
                        }
                        needs_redraw = true;
                    }
                    // l/Right — next option (SegmentedControl) or toggle (Toggle)
                    Key::Char('l') | Key::Named(NamedKey::Right)
                        if self.active_view == SidebarView::Settings =>
                    {
                        if self.settings_change_focused(1) {
                            needs_redraw = true;
                        }
                    }
                    // h/Left — previous option
                    Key::Char('h') | Key::Named(NamedKey::Left)
                        if self.active_view == SidebarView::Settings =>
                    {
                        if self.settings_change_focused(-1) {
                            needs_redraw = true;
                        }
                    }
                    // Space/Enter — toggle or select current field
                    Key::Char(' ') | Key::Named(NamedKey::Enter)
                        if self.active_view == SidebarView::Settings =>
                    {
                        if self.settings_change_focused(1) {
                            needs_redraw = true;
                        }
                    }

                    // ── Tab — cycle sections within Board SidebarSystem ──
                    Key::Named(NamedKey::Tab)
                        if self.active_view == SidebarView::Board =>
                    {
                        let prev_sel = self.board_selected_issue();
                        let result = self.board_sidebar.handle(&event, backend, list_b);
                        if result != SidebarEvent::Ignored {
                            let new_sel = self.board_selected_issue();
                            if new_sel != prev_sel {
                                self.detail_scroll = 0;
                            }
                            needs_redraw = true;
                        }
                    }

                    // ── j/k — scroll Issue tab body ───────────────────────
                    Key::Char('j') | Key::Named(NamedKey::Down)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Issue =>
                    {
                        self.pipeline_detail_scroll = self.pipeline_detail_scroll.saturating_add(1);
                        needs_redraw = true;
                    }
                    Key::Char('k') | Key::Named(NamedKey::Up)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Issue =>
                    {
                        self.pipeline_detail_scroll = self.pipeline_detail_scroll.saturating_sub(1);
                        needs_redraw = true;
                    }

                    // ── Down / j ─────────────────────────────────────────
                    Key::Char('j') | Key::Named(NamedKey::Down) => {
                        match self.active_view {
                            SidebarView::Board => {
                                let prev_sel = self.board_selected_issue();
                                let result = self.board_sidebar.handle(&event, backend, list_b);
                                if result != SidebarEvent::Ignored {
                                    let new_sel = self.board_selected_issue();
                                    if new_sel != prev_sel {
                                        self.detail_scroll = 0;
                                    }
                                }
                            }
                            SidebarView::Machines => {
                                let m = self.data.machines.len();
                                if m > 0 && self.machine_sel + 1 < m {
                                    self.machine_sel += 1;
                                    self.machine_detail_scroll = 0;
                                }
                                self.fix_machine_scroll(content_visible_rows(list_b, lh));
                            }
                            SidebarView::Pipeline => {
                                let prev = self.pipeline_sel;
                                self.pipeline_sidebar.handle(&event, backend, list_b);
                                self.pipeline_sel = self.selected_pipeline_index();
                                if self.pipeline_sel != prev {
                                    self.pipeline_detail_scroll = 0;
                                }
                            }
                            // Settings: handled by the earlier guarded arm.
                            SidebarView::Settings => {}
                        }
                        needs_redraw = true;
                    }

                    // ── Up / k ───────────────────────────────────────────
                    Key::Char('k') | Key::Named(NamedKey::Up) => {
                        match self.active_view {
                            SidebarView::Board => {
                                let prev_sel = self.board_selected_issue();
                                let result = self.board_sidebar.handle(&event, backend, list_b);
                                if result != SidebarEvent::Ignored {
                                    let new_sel = self.board_selected_issue();
                                    if new_sel != prev_sel {
                                        self.detail_scroll = 0;
                                    }
                                }
                            }
                            SidebarView::Machines => {
                                if self.machine_sel > 0 {
                                    self.machine_sel -= 1;
                                    self.machine_detail_scroll = 0;
                                }
                                self.fix_machine_scroll(content_visible_rows(list_b, lh));
                            }
                            SidebarView::Pipeline => {
                                let prev = self.pipeline_sel;
                                self.pipeline_sidebar.handle(&event, backend, list_b);
                                self.pipeline_sel = self.selected_pipeline_index();
                                if self.pipeline_sel != prev {
                                    self.pipeline_detail_scroll = 0;
                                }
                            }
                            // Settings: handled by the earlier guarded arm.
                            SidebarView::Settings => {}
                        }
                        needs_redraw = true;
                    }

                    // ── [ / ] — cycle focused stage on the Stages tab ────
                    // Sets `pipeline_focused_stage`, which the rasteriser
                    // draws with an accent border, and selects which
                    // stage's content the scrollable panel shows.
                    Key::Char('[')
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Stages =>
                    {
                        self.focus_prev_pipeline_stage();
                        needs_redraw = true;
                    }
                    Key::Char(']')
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Stages =>
                    {
                        self.focus_next_pipeline_stage();
                        needs_redraw = true;
                    }

                    // ── h/l — cycle Pipeline detail tabs ─────────────────
                    // Order: Pipeline → Issue → Stages → Pipeline …
                    Key::Char('h') | Key::Named(NamedKey::Left)
                        if self.active_view == SidebarView::Pipeline =>
                    {
                        self.pipeline_detail_tab = match self.pipeline_detail_tab {
                            PipelineDetailTab::Pipeline => PipelineDetailTab::Stages,
                            PipelineDetailTab::Issue => PipelineDetailTab::Pipeline,
                            PipelineDetailTab::Stages => PipelineDetailTab::Issue,
                        };
                        self.pipeline_detail_scroll = 0;
                        needs_redraw = true;
                    }
                    Key::Char('l') | Key::Named(NamedKey::Right)
                        if self.active_view == SidebarView::Pipeline =>
                    {
                        self.pipeline_detail_tab = match self.pipeline_detail_tab {
                            PipelineDetailTab::Pipeline => PipelineDetailTab::Issue,
                            PipelineDetailTab::Issue => PipelineDetailTab::Stages,
                            PipelineDetailTab::Stages => PipelineDetailTab::Pipeline,
                        };
                        self.pipeline_detail_scroll = 0;
                        needs_redraw = true;
                    }

                    // ── h/l — cycle Board detail tabs ────────────────────
                    // Board ↔ Issue (no third tab — toggle on either key).
                    Key::Char('h') | Key::Char('l')
                    | Key::Named(NamedKey::Left)
                    | Key::Named(NamedKey::Right)
                        if self.active_view == SidebarView::Board
                            && !self.board_search_focused =>
                    {
                        self.board_detail_tab = match self.board_detail_tab {
                            BoardDetailTab::Board => BoardDetailTab::Issue,
                            BoardDetailTab::Issue => BoardDetailTab::Board,
                        };
                        self.detail_scroll = 0;
                        needs_redraw = true;
                    }

                    // ── Home ─────────────────────────────────────────────
                    Key::Named(NamedKey::Home) => {
                        match self.active_view {
                            SidebarView::Board => {
                                let prev_sel = self.board_selected_issue();
                                self.board_sidebar.handle(&event, backend, list_b);
                                let new_sel = self.board_selected_issue();
                                if new_sel != prev_sel {
                                    self.detail_scroll = 0;
                                }
                            }
                            SidebarView::Machines => {
                                self.machine_sel = 0;
                                self.machine_detail_scroll = 0;
                                self.fix_machine_scroll(content_visible_rows(list_b, lh));
                            }
                            SidebarView::Pipeline => {
                                self.pipeline_sidebar.handle(&event, backend, list_b);
                                self.pipeline_sel = self.selected_pipeline_index();
                            }
                            SidebarView::Settings => {
                                // #237: jump to the first interactive field
                                // in the unified form.
                                self.settings_field_sel = 0;
                                self.settings_form.borrow_mut().set_scroll_offset(0);
                            }
                        }
                        needs_redraw = true;
                    }

                    // ── End ──────────────────────────────────────────────
                    Key::Named(NamedKey::End) => {
                        match self.active_view {
                            SidebarView::Board => {
                                let prev_sel = self.board_selected_issue();
                                self.board_sidebar.handle(&event, backend, list_b);
                                let new_sel = self.board_selected_issue();
                                if new_sel != prev_sel {
                                    self.detail_scroll = 0;
                                }
                            }
                            SidebarView::Machines => {
                                let m = self.data.machines.len();
                                if m > 0 {
                                    self.machine_sel = m - 1;
                                    self.machine_detail_scroll = 0;
                                }
                                self.fix_machine_scroll(content_visible_rows(list_b, lh));
                            }
                            SidebarView::Pipeline => {
                                self.pipeline_sidebar.handle(&event, backend, list_b);
                                self.pipeline_sel = self.selected_pipeline_index();
                            }
                            SidebarView::Settings => {
                                // #237: jump to the last interactive field
                                // in the unified form.
                                let count = self.settings_interactive_field_ids().len();
                                self.settings_field_sel = count.saturating_sub(1);
                            }
                        }
                        needs_redraw = true;
                    }

                    // ── Enter — Stages tab: open watch overlay for the
                    //              issue's running assignment. Other
                    //              tabs: fire Go on the active stage.
                    Key::Named(NamedKey::Enter)
                        if self.active_view == SidebarView::Pipeline =>
                    {
                        if self.pipeline_detail_tab == PipelineDetailTab::Stages {
                            self.open_watch_for_selected_issue();
                        } else {
                            self.dispatch_pipeline_active_go();
                        }
                        needs_redraw = true;
                    }

                    // ── r — mark refined issue ready for dispatch ────────
                    // For an issue with status:refining (or status:backlog,
                    // or no status:* label), `r` spawns `coord ready` which
                    // sets status:ready via gh. After the GH side returns,
                    // the next data refresh moves the row into the Pending
                    // lifecycle section and the Pipeline tab shows [Go].
                    Key::Char('r')
                        if self.active_view == SidebarView::Pipeline =>
                    {
                        // #249 Principle 2: every no-op gives feedback.
                        // Without these toasts, pressing `r` on an issue
                        // with no coord_repo mapping (or no selection)
                        // looks identical to pressing `r` on a working
                        // issue — both render nothing, and the user is
                        // left guessing.
                        let selected = self
                            .pipeline_sel
                            .and_then(|i| self.pipeline_issues.get(i));
                        match selected {
                            None => {
                                self.push_toast(
                                    "Nothing to mark ready",
                                    "Select an issue in the pipeline first.",
                                    ToastSeverity::Info,
                                );
                            }
                            Some(issue) if issue.coord_repo.is_none() => {
                                self.push_toast(
                                    "No coord_repo mapping",
                                    &format!(
                                        "{} isn't mapped in coordinator.yml — \
                                         add a `repos` entry so coord can act on it.",
                                        issue.repo_slug,
                                    ),
                                    ToastSeverity::Warning,
                                );
                            }
                            Some(issue) => {
                                let repo = issue.coord_repo.clone().unwrap();
                                let num = issue.number;
                                let num_str = num.to_string();
                                if self.command_runner.spawn(&["ready", &repo, &num_str]) {
                                    self.pipeline_status = Some((
                                        format!("#{}: marking ready", num),
                                        Instant::now(),
                                    ));
                                } else {
                                    self.push_toast(
                                        "Another command is running",
                                        "Wait for the current command to finish before pressing r.",
                                        ToastSeverity::Info,
                                    );
                                }
                            }
                        }
                        needs_redraw = true;
                    }

                    // ── PageDown (Board only) ─────────────────────────────
                    Key::Named(NamedKey::PageDown)
                        if self.active_view == SidebarView::Board =>
                    {
                        let prev_sel = self.board_selected_issue();
                        self.board_sidebar.handle(&event, backend, list_b);
                        let new_sel = self.board_selected_issue();
                        if new_sel != prev_sel {
                            self.detail_scroll = 0;
                        }
                        needs_redraw = true;
                    }

                    // ── PageUp (Board only) ───────────────────────────────
                    Key::Named(NamedKey::PageUp)
                        if self.active_view == SidebarView::Board =>
                    {
                        let prev_sel = self.board_selected_issue();
                        self.board_sidebar.handle(&event, backend, list_b);
                        let new_sel = self.board_selected_issue();
                        if new_sel != prev_sel {
                            self.detail_scroll = 0;
                        }
                        needs_redraw = true;
                    }

                    // ── Arrow cursor movement inside the search box ───────
                    Key::Named(NamedKey::Left)
                        if self.active_view == SidebarView::Board
                            && self.board_search_focused =>
                    {
                        // Move cursor one Unicode scalar left.
                        let chars: Vec<char> = self.board_search.chars().collect();
                        let char_pos = chars.len().min(self.board_search_cursor);
                        if char_pos > 0 {
                            self.board_search_cursor =
                                chars[..char_pos - 1].iter().collect::<String>().len();
                        }
                        self.rebuild_board_sidebar();
                        needs_redraw = true;
                    }
                    Key::Named(NamedKey::Right)
                        if self.active_view == SidebarView::Board
                            && self.board_search_focused =>
                    {
                        let max = self.board_search.len();
                        if self.board_search_cursor < max {
                            // Advance past the next char boundary.
                            let rest = &self.board_search[self.board_search_cursor..];
                            if let Some(ch) = rest.chars().next() {
                                self.board_search_cursor += ch.len_utf8();
                            }
                        }
                        self.rebuild_board_sidebar();
                        needs_redraw = true;
                    }

                    Key::Char('r') => {
                        self.refresh();
                        self.kick_issue_sync();
                        needs_redraw = true;
                    }

                    // ── Coordinator commands ─────────────────────────────
                    // #192: `p` / `a` / `A` are retired alongside the
                    // PROPOSALS section.  Right-click → Send to
                    // Pipeline (#261) is the canonical dispatch path
                    // now; `coord plan` still exists as a CLI escape
                    // hatch but doesn't earn a keybind.
                    Key::Char('n') => {
                        self.command_runner.spawn(&["notify"]);
                        self.last_notify = Instant::now();
                        needs_redraw = true;
                    }
                    Key::Char('m') => {
                        // #272-followup: route everything through the
                        // classifier so silent no-ops become actionable
                        // toasts (review blocked → toast; CI failed →
                        // open the force-merge prompt; ready → spawn
                        // `coord merge --repo <slug>`).
                        if self.dispatch_pipeline_merge_for_selected_issue() {
                            needs_redraw = true;
                        }
                    }
                    // #253: Capital M overrides the review-approval gate —
                    // active only when the selected merge entry is blocked
                    // on review so the keybind doesn't silently skip review
                    // on unrelated merges.
                    Key::Char('M')
                        if self.merge_blocked_on_review_for_selected_issue() =>
                    {
                        self.command_runner.spawn(&["merge", "--skip-review"]);
                        needs_redraw = true;
                    }
                    Key::Char('R') => {
                        if self.active_view == SidebarView::Pipeline {
                            // #236: Test failed → R bounces back to a fresh
                            // Work dispatch (the Pipeline widget can't attach
                            // a [Retry] to a Failed Test, so without this the
                            // R keybind has no actionable target).
                            if self.can_bounce_work_after_test_fail() {
                                self.dispatch_pipeline_work();
                            } else if self.dispatch_pipeline_active_go() {
                                // In the Pipeline panel, R fires the active
                                // stage button — Retry on a Failed stage, or
                                // Go on a Pending one (same as Enter). When
                                // it dispatched something, we're done.
                            } else {
                                // #194: when no stage is actionable, fall back
                                // to an immediate refresh of pipeline issues
                                // from GitHub. Reset the last-load timestamp so
                                // maybe_kick_pipeline_loader() bypasses the 60 s
                                // guard. This matches the `R=refresh` hint
                                // shown in the Pipeline status bar.
                                self.pipeline_last_load = None;
                                self.maybe_kick_pipeline_loader();
                            }
                            needs_redraw = true;
                        } else if let Some(a) = self.board_selected_failed_assignment() {
                            let id = a.id.clone();
                            self.command_runner.spawn(&["retry", &id]);
                            needs_redraw = true;
                        } else {
                            // #249 Principle 2: explain the precondition
                            // for Board-view R so users don't think the
                            // keybind is broken.
                            self.push_toast(
                                "No failed assignment selected",
                                "Focus a row with status FAIL in the Board sidebar, then press R.",
                                ToastSeverity::Info,
                            );
                            needs_redraw = true;
                        }
                    }

                    // ── P — Purge done/failed assignments older than purge_days ──
                    // Only fires in the Board view when the cursor is in the
                    // Completed (done/merged) status group.  Opens a confirm
                    // prompt; the early-intercept block above handles 'y'/cancel.
                    Key::Char('P')
                        if self.active_view == SidebarView::Board
                            && !self.board_search_focused
                            && self.board_selection_in_completed_group() =>
                    {
                        let secs = self.purge_days as f64 * 86_400.0;
                        let counts = count_purgeable_db(secs).unwrap_or((0, 0));
                        self.pending_purge = Some(counts);
                        needs_redraw = true;
                    }

                    // ── D — Dismiss a Done-section pipeline issue (session-only) ──
                    // Hides the selected issue from the Done section for the lifetime
                    // of the current TUI session.  The issue reappears after a restart
                    // or if the user manually re-runs `gh` (i.e. this is in-memory only).
                    // Only fires when the selected issue is in the Done lifecycle section,
                    // preventing accidental dismissal of active work.
                    Key::Char('D')
                        if self.active_view == SidebarView::Pipeline =>
                    {
                        if let Some(idx) = self.pipeline_sel {
                            if let Some(issue) = self.pipeline_issues.get(idx).cloned() {
                                if self.pipeline_lifecycle_section(&issue) == "done" {
                                    self.pipeline_dismissed
                                        .insert((issue.repo_slug.clone(), issue.number));
                                    // The dismissed issue is filtered out by
                                    // pipeline_groups_for_repo; pass None so the
                                    // rebuild lands on a sensible neighbor.
                                    self.rebuild_pipeline_sidebar(None);
                                    needs_redraw = true;
                                } else {
                                    self.pipeline_status = Some((
                                        format!(
                                            "D only dismisses Done issues (#{} is {})",
                                            issue.number,
                                            self.pipeline_lifecycle_section(&issue)
                                        ),
                                        Instant::now(),
                                    ));
                                    needs_redraw = true;
                                }
                            }
                        }
                    }

                    // ── #200 Test gate: P = Pass, F = Fail (reason), S = Skip ──
                    // Active in the Pipeline view when the selected issue's Test
                    // stage is Pending (Work is Done, no verdict yet).
                    Key::Char('P')
                        if self.active_view == SidebarView::Pipeline
                            && self.pending_test_fail.is_none()
                            && self.test_gate_actionable() =>
                    {
                        self.record_test_verdict("passed", None);
                        needs_redraw = true;
                    }
                    Key::Char('S')
                        if self.active_view == SidebarView::Pipeline
                            && self.pending_test_fail.is_none()
                            && self.test_gate_actionable() =>
                    {
                        self.record_test_verdict("skipped", None);
                        needs_redraw = true;
                    }
                    Key::Char('F')
                        if self.active_view == SidebarView::Pipeline
                            && self.pending_test_fail.is_none()
                            && self.test_gate_actionable() =>
                    {
                        // Open inline reason input. We need a stable handle for
                        // the work assignment in case the list reshuffles.
                        if let Some(_work_id) = self.pipeline_selected_work_id() {
                            // We carry an unused 0 as the first tuple slot —
                            // pipeline_selected_work_id() is re-resolved at
                            // submit time, so we don't need to cache the index.
                            self.pending_test_fail = Some((0, String::new()));
                            needs_redraw = true;
                        }
                    }

                    // #bounce: lowercase f = bounce the pipeline back
                    // to a fix worker when the selected Pipeline row has
                    // a request-changes review.  Uppercase F is the
                    // Test-fail key (handled above) and only fires when
                    // the test gate is actionable, so the two don't
                    // overlap in practice.
                    Key::Char('f')
                        if self.active_view == SidebarView::Pipeline
                            && self.selected_pipeline_review_id_for_bounce().is_some() =>
                    {
                        self.dispatch_bounce_for_selected_pipeline_row();
                        needs_redraw = true;
                    }

                    // ── #235 Phase 1: B = build (fetch + checkout +
                    //              build_command on the local machine) ──
                    // Spawns `coord test <work_id>` in a background thread
                    // and toasts the outcome. Manual trigger by design —
                    // auto-on-completion would clobber the user's working
                    // copy mid-edit.
                    Key::Char('B')
                        if self.pending_test_fail.is_none()
                            && self.can_trigger_test_build() =>
                    {
                        if let Some(work_id) = self.pipeline_selected_work_id() {
                            let (branch, issue_number) = self
                                .data
                                .assignments
                                .iter()
                                .find(|a| a.id == work_id)
                                .and_then(|a| a.branch.clone().map(|b| (b, a.issue_number)))
                                .unwrap_or_else(|| (String::from("?"), 0));
                            self.spawn_test_build(work_id, branch, issue_number);
                            needs_redraw = true;
                        }
                    }

                    _ => {}
                }
            }

            UiEvent::WindowResized { .. } => { needs_redraw = true; }

            _ => {}
        }

        if needs_redraw {
            Reaction::Redraw
        } else {
            Reaction::Continue
        }
    }

    /// Sync `active_view` when the shell switches panels via activity bar click.
    fn on_shell_event(&mut self, event: &AppShellEvent) {
        if let AppShellEvent::PanelChanged { panel_id } = event {
            self.active_view = match panel_id.as_str() {
                "panel:board" => SidebarView::Board,
                "panel:machines" => SidebarView::Machines,
                "panel:pipeline" => {
                    self.maybe_kick_pipeline_loader();
                    SidebarView::Pipeline
                }
                "panel:settings" => SidebarView::Settings,
                _ => return,
            };
        }
    }

    /// Periodic callback driven by the quadraui runner (~60Hz on TUI).
    /// Does the same time-based work as `handle()` so background refreshes,
    /// command-runner draining, and watch-log polling proceed even when the
    /// user isn't typing.
    fn tick(&mut self, _backend: &mut dyn Backend) -> Reaction {
        if self.run_periodic_work() {
            Reaction::Redraw
        } else {
            Reaction::Continue
        }
    }
}

/// Estimate the number of visible rows in a `ListView` panel.
///
/// Deducts one row for the panel title strip.
fn content_visible_rows(panel: Rect, lh: f32) -> usize {
    if lh <= 0.0 {
        return 10;
    }
    let content_h = (panel.height - lh).max(0.0); // minus list title row
    (content_h / lh) as usize
}

/// Carve out the rect used by the PipelineView primitive at the top of the
/// Pipeline detail pane.  Reserves 6 rows by default (icon row + label row
/// + action row + 1 row of padding/border), clamped to ≤ 50 % of the
/// available height so the issue summary below remains visible.
fn pipeline_detail_pv_rect(main: Rect, lh: f32) -> Rect {
    if lh <= 0.0 {
        return Rect::new(main.x, main.y, main.width, 0.0);
    }
    let want_rows = 6.0_f32;
    let max_h = (main.height * 0.55).max(lh);
    let h = (want_rows * lh).min(max_h);
    Rect::new(main.x, main.y, main.width, h)
}

/// Render an issue's GitHub body as a ListView. Shared between the Pipeline
/// view's Issue tab and the Board view's Issue tab so the rendering and
/// scroll handling stay in lock-step.
///
/// `issue` is `Some((number, title, body, labels))` for the selected issue,
/// or `None` when no issue is selected (renders a placeholder).
fn issue_body_list(
    issue: Option<(u64, &str, &str, &[String])>,
    scroll_offset: usize,
    widget_id: &'static str,
) -> ListView {
    let mut items: Vec<ListItem> = Vec::new();
    match issue {
        None => {
            items.push(kv_item("", " No issue selected", Some(Color::rgb(100, 100, 100))));
        }
        Some((number, title, body, labels)) => {
            items.push(ListItem {
                text: StyledText {
                    spans: vec![
                        StyledSpan::with_fg(format!(" #{}", number), Color::rgb(150, 150, 240)),
                        StyledSpan::with_fg(format!("  {}", title), Color::rgb(230, 230, 255)),
                    ],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Header,
            });
            if !labels.is_empty() {
                items.push(kv_item(
                    "",
                    &format!(" labels: {}", labels.join(", ")),
                    Some(Color::rgb(160, 160, 180)),
                ));
            }
            items.push(kv_item("", "", None));
            if body.is_empty() {
                items.push(kv_item("", " (no description)", Some(Color::rgb(100, 100, 100))));
            } else {
                for line in body.lines() {
                    items.push(kv_item("", &format!(" {}", line), Some(Color::rgb(200, 200, 210))));
                }
            }
        }
    }
    ListView {
        id: WidgetId::new(widget_id),
        title: None,
        items,
        selected_idx: 0,
        scroll_offset,
        has_focus: false,
        bordered: false,
    }
}

// ─── Settings helpers ─────────────────────────────────────────────────────────

/// Build a non-interactive category label `FormField` for the settings form.
fn settings_label(text: &str) -> FormField {
    FormField {
        id: WidgetId::new(format!("settings-label:{}", text.to_lowercase().replace(' ', "-"))),
        label: StyledText::plain(text.to_string()),
        kind: FieldKind::Label,
        hint: StyledText::default(),
        disabled: false,
        validation: None,
    }
}

// ─── Purge helper ─────────────────────────────────────────────────────────────

/// Open a short-lived read-write connection to `coord.db` and delete:
///
/// * `assignments` rows where `status IN ('done', 'failed')` and
///   `finished_at < now - older_than_secs`
/// * `issues` rows where `state = 'closed'` and
///   `synced_at < now - older_than_secs`
///
/// Returns the total number of rows deleted across both tables.
///
/// A separate read-write connection is used because the main data-load
/// connection is opened with `SQLITE_OPEN_READ_ONLY`.  SQLite WAL mode
/// serialises concurrent writers, so this is safe.
/// Compute the cutoff timestamp for purge predicates.
fn purge_cutoff(older_than_secs: f64) -> f64 {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    now - older_than_secs
}

/// Open a writer connection with a 5s busy timeout so a brief lock from
/// the coordinator doesn't make purge silently no-op.
fn open_purge_conn() -> rusqlite::Result<Connection> {
    let db_path = coord_dir().join("coord.db");
    let conn = Connection::open(&db_path)?;
    conn.busy_timeout(Duration::from_millis(5000))?;
    Ok(conn)
}

/// Count rows that would be deleted by [`purge_done_assignments_conn`].
/// Inner helper that takes an explicit connection so tests can run against
/// an in-memory DB without touching the real coord.db.
fn count_purgeable_conn(conn: &Connection, cutoff: f64) -> rusqlite::Result<(usize, usize)> {
    let a: i64 = conn.query_row(
        "SELECT COUNT(*) FROM assignments \
         WHERE status IN ('done', 'failed') \
         AND finished_at IS NOT NULL \
         AND finished_at < ?1",
        rusqlite::params![cutoff],
        |r| r.get(0),
    )?;
    let i: i64 = conn.query_row(
        "SELECT COUNT(*) FROM issues \
         WHERE state = 'closed' \
         AND synced_at IS NOT NULL \
         AND synced_at < ?1",
        rusqlite::params![cutoff],
        |r| r.get(0),
    )?;
    Ok((a as usize, i as usize))
}

/// Delete old `done`/`failed` assignments and old closed issues.
/// Inner helper — see [`count_purgeable_conn`].
fn purge_done_assignments_conn(conn: &Connection, cutoff: f64) -> rusqlite::Result<(usize, usize)> {
    let assignments_deleted = conn.execute(
        "DELETE FROM assignments \
         WHERE status IN ('done', 'failed') \
         AND finished_at IS NOT NULL \
         AND finished_at < ?1",
        rusqlite::params![cutoff],
    )?;
    let issues_deleted = conn.execute(
        "DELETE FROM issues \
         WHERE state = 'closed' \
         AND synced_at IS NOT NULL \
         AND synced_at < ?1",
        rusqlite::params![cutoff],
    )?;
    Ok((assignments_deleted, issues_deleted))
}

/// Count rows that would be deleted by [`purge_done_assignments_db`].
///
/// Returns `(assignments, closed_issues)` so the confirmation prompt and the
/// completion toast show matching numbers — the user is never surprised by
/// a "47 rows removed" toast after confirming "Purge 3 rows".
fn count_purgeable_db(older_than_secs: f64) -> rusqlite::Result<(usize, usize)> {
    let conn = open_purge_conn()?;
    count_purgeable_conn(&conn, purge_cutoff(older_than_secs))
}

/// Delete old `done`/`failed` assignments and old closed issues.
/// Returns `(assignments_deleted, issues_deleted)`; errors propagate to the
/// caller for a visible error toast (silent `.unwrap_or(0)` previously hid
/// SQLITE_BUSY).
fn purge_done_assignments_db(older_than_secs: f64) -> rusqlite::Result<(usize, usize)> {
    let conn = open_purge_conn()?;
    purge_done_assignments_conn(&conn, purge_cutoff(older_than_secs))
}

/// #200: Record a Test gate verdict on the given work assignment id.
/// `verdict` is "passed" | "failed" | "skipped". `reason` is only stored for
/// failures (ignored otherwise).
fn record_test_verdict_db(
    assignment_id: &str,
    verdict: &str,
    reason: Option<&str>,
) -> rusqlite::Result<()> {
    let conn = open_purge_conn()?;
    conn.execute(
        "UPDATE assignments SET test_state = ?1, test_reason = ?2 WHERE assignment_id = ?3",
        rusqlite::params![verdict, reason, assignment_id],
    )?;
    Ok(())
}

// ─── Unit tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    /// Collect every `ToolbarButton::Action` label from a toolbar.
    /// Test-only convenience for assertions that previously walked
    /// `bar.left_segments` on the StatusBar shape.
    fn toolbar_action_labels(bar: &Toolbar) -> Vec<String> {
        bar.buttons
            .iter()
            .filter_map(|b| match b {
                ToolbarButton::Action { label, .. } => Some(label.clone()),
                _ => None,
            })
            .collect()
    }

    /// Collect every action_id (as raw string) from a toolbar.
    fn toolbar_action_ids(bar: &Toolbar) -> Vec<String> {
        bar.buttons
            .iter()
            .filter_map(|b| match b {
                ToolbarButton::Action { id, .. } => Some(id.as_str().to_string()),
                _ => None,
            })
            .collect()
    }

    // ── fmt_dur ────────────────────────────────────────────────────────────────

    #[test]
    fn fmt_dur_zero_seconds() {
        assert_eq!(fmt_dur(0), "0s");
    }

    #[test]
    fn fmt_dur_fifty_nine_seconds() {
        assert_eq!(fmt_dur(59), "59s");
    }

    #[test]
    fn fmt_dur_exactly_one_minute() {
        assert_eq!(fmt_dur(60), "1m");
    }

    #[test]
    fn fmt_dur_exactly_one_hour() {
        assert_eq!(fmt_dur(3600), "1h0m");
    }

    #[test]
    fn fmt_dur_one_hour_one_minute_one_second() {
        // 3661 = 1h 1m 1s → displayed as "1h1m" (seconds dropped)
        assert_eq!(fmt_dur(3661), "1h1m");
    }

    // ── trunc ──────────────────────────────────────────────────────────────────

    #[test]
    fn trunc_ascii_within_limit() {
        assert_eq!(trunc("hello", 10), "hello");
    }

    #[test]
    fn trunc_ascii_over_limit() {
        assert_eq!(trunc("hello world", 5), "hello");
    }

    #[test]
    fn trunc_empty_string() {
        assert_eq!(trunc("", 5), "");
    }

    #[test]
    fn trunc_multibyte_no_panic() {
        // "🎉" is 4 UTF-8 bytes.  The old `&s[..3]` would have panicked by
        // splitting the emoji in the middle.  The new char-index implementation
        // must return the correct 3-character prefix cleanly.
        let s = "🎉 hello";
        // chars: ['🎉', ' ', 'h', 'e', 'l', 'l', 'o']
        // first 3 chars → "🎉 h"
        assert_eq!(trunc(s, 3), "🎉 h");
    }

    // ── Board helpers ──────────────────────────────────────────────────────

    fn make_test_app(data: BoardData) -> CoordApp {
        let mut sidebar = SidebarSystem::new(Vec::new());
        sidebar.set_navigation_mode(NavigationMode::Selection);
        sidebar.set_allow_collapse(true);
        let mut pipeline_sidebar = SidebarSystem::new(Vec::new());
        pipeline_sidebar.set_navigation_mode(NavigationMode::Selection);
        pipeline_sidebar.set_allow_collapse(true);
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
            command_runner: crate::commands::CommandRunner::new(),
            last_notify: Instant::now(),
            issue_sync_last: None,
            board_search: String::new(),
            board_search_cursor: 0,
            board_search_focused: false,
            board_status_expanded: std::collections::HashMap::new(),
            pipeline_sidebar,
            pipeline_repo_names: Vec::new(),
            pipeline_issues: Vec::new(),
            pipeline_sel: None,
            pipeline_loader: None,
            pipeline_last_load: None,
            pipeline_status: None,
            toasts: Vec::new(),
            next_toast_id: 0,
            watch: None,
            inject_input: String::new(),
            inject_focused: false,
            pipeline_detail_tab: PipelineDetailTab::default(),
            board_detail_tab: BoardDetailTab::default(),
            pipeline_detail_scroll: 0,
            remote_log_cache: std::cell::RefCell::new(std::collections::HashMap::new()),
            pending_data: None,
            fetch_error: None,
            pending_log_fetches: std::cell::RefCell::new(std::collections::HashMap::new()),
            pending_issue_fetches: std::cell::RefCell::new(std::collections::HashMap::new()),
            fetched_issues_cache: std::cell::RefCell::new(std::collections::HashMap::new()),
            pending_purge: None,
            pending_test_fail: None,
            pending_force_merge: None,
            pending_context_menu: None,
            context_menu_layout: std::cell::RefCell::new(None),
            last_main_visible_rows: std::cell::Cell::new(40),
            purge_days: 7,
            watch_sse: None,
            sidebar_action_bar_hover: ToolbarHoverTracker::new(),
            panel_toolbar_hover: ToolbarHoverTracker::new(),
            pipeline_focused_stage: None,
            pipeline_stage_content_scroll: 0,
            settings: TuiSettings::default(),
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
        }
    }

    fn make_app_default() -> CoordApp {
        make_test_app(BoardData::default())
    }

    fn make_app_with_assignments(assignments: Vec<Assignment>) -> CoordApp {
        let mut app = make_test_app(BoardData {
            assignments,
            ..BoardData::default()
        });
        app.rebuild_board_sidebar();
        app
    }

    // ── issues_by_repo ──────────────────────────────────────────────────────

    #[test]
    fn issues_by_repo_empty_data() {
        let app = make_app_default();
        let grouped = app.issues_by_repo();
        assert!(grouped.is_empty());
    }

    #[test]
    fn issues_by_repo_groups_by_repo() {
        let assignments = vec![
            make_assignment_typed("running", 10, "repo-a", Some("work")),
            make_assignment_typed("done", 10, "repo-a", Some("review")),
            make_assignment_typed("done", 20, "repo-b", Some("work")),
        ];
        let app = make_app_with_assignments(assignments);
        let grouped = app.issues_by_repo();
        assert_eq!(grouped.len(), 2);
        // repo-a has running issue → sorted first.
        let (repo_a_name, repo_a_issues) = &grouped[0];
        assert_eq!(repo_a_name, "repo-a");
        assert_eq!(repo_a_issues.len(), 1); // issue #10
        assert_eq!(repo_a_issues[0].issue_number, 10);
        assert_eq!(repo_a_issues[0].assignments.len(), 2);
        assert_eq!(repo_a_issues[0].status_summary, "running");

        let (repo_b_name, repo_b_issues) = &grouped[1];
        assert_eq!(repo_b_name, "repo-b");
        assert_eq!(repo_b_issues.len(), 1); // issue #20
        assert_eq!(repo_b_issues[0].status_summary, "done");
    }

    #[test]
    fn issues_by_repo_status_failed_when_latest_failed() {
        let assignments = vec![
            make_assignment_typed("done", 10, "repo", Some("work")),
            make_assignment_typed("failed", 10, "repo", Some("review")),
        ];
        let app = make_app_with_assignments(assignments);
        let grouped = app.issues_by_repo();
        assert_eq!(grouped[0].1[0].status_summary, "failed");
    }

    #[test]
    fn board_selected_issue_none_when_no_selection() {
        let app = make_app_default();
        assert!(app.board_selected_issue().is_none());
    }

    #[test]
    fn board_selected_issue_returns_correct_issue() {
        let assignments = vec![
            make_assignment_typed("running", 10, "repo-a", Some("work")),
            make_assignment_typed("done", 20, "repo-b", Some("work")),
        ];
        let mut app = make_app_with_assignments(assignments);
        // Section 0 = search form; section 1 = repo-a (running first).
        // Path is 2-level: [group_idx=0 (Running), issue_idx=0].
        app.board_sidebar.set_active_section(Some(1));
        app.board_sidebar.set_selected_path(1, Some(vec![0, 0]));
        let sel = app.board_selected_issue();
        assert!(sel.is_some(), "expected Some, got None");
        let (repo, issue_num) = sel.unwrap();
        assert_eq!(repo, "repo-a");
        assert_eq!(issue_num, 10);
    }

    // ── fix_machine_scroll ────────────────────────────────────────────────────

    fn make_app_machine(machine_sel: usize, machine_scroll: usize) -> CoordApp {
        let mut app = make_test_app(BoardData::default());
        app.active_view = SidebarView::Machines;
        app.machine_sel = machine_sel;
        app.machine_scroll = machine_scroll;
        app
    }

    #[test]
    fn fix_machine_scroll_within_visible_window() {
        let mut d = make_app_machine(3, 0);
        d.fix_machine_scroll(10);
        assert_eq!(d.machine_scroll, 0);
    }

    #[test]
    fn fix_machine_scroll_past_end_of_window() {
        let mut d = make_app_machine(12, 0);
        d.fix_machine_scroll(10);
        assert_eq!(d.machine_scroll, 3);
    }

    #[test]
    fn fix_machine_scroll_before_scroll_offset() {
        let mut d = make_app_machine(0, 5);
        d.fix_machine_scroll(10);
        assert_eq!(d.machine_scroll, 0);
    }

    // ── SidebarView ───────────────────────────────────────────────────────────

    #[test]
    fn sidebar_view_label() {
        assert_eq!(SidebarView::Board.label(), "Board");
        assert_eq!(SidebarView::Machines.label(), "Machines");
    }

    #[test]
    fn sidebar_view_default_is_board() {
        assert_eq!(SidebarView::default(), SidebarView::Board);
    }

    // ── Issue grouping (replaces old pipeline tests) ─────────────────────────

    fn make_assignment_typed(status: &str, issue: u64, repo: &str, atype: Option<&str>) -> Assignment {
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
        }
    }

    #[test]
    fn issues_by_repo_deduplicates_issues() {
        let assignments = vec![
            make_assignment_typed("running", 10, "repo-a", Some("work")),
            make_assignment_typed("done", 10, "repo-a", Some("plan")),
            make_assignment_typed("done", 20, "repo-b", Some("work")),
        ];
        let app = make_app_with_assignments(assignments);
        let grouped = app.issues_by_repo();
        // Two repos, one issue each.
        let total_issues: usize = grouped.iter().map(|(_, issues)| issues.len()).sum();
        assert_eq!(total_issues, 2);
    }

    #[test]
    fn issues_by_repo_includes_empty_repos_from_machines() {
        let mut app = make_test_app(BoardData {
            machines: vec![Machine {
                name: "m1".to_string(),
                host: String::new(),
                reachable: true,
                active_count: 0,
                repos: vec!["empty-repo".to_string()],
            }],
            ..BoardData::default()
        });
        app.rebuild_board_sidebar();
        let grouped = app.issues_by_repo();
        // Empty repo should appear with 0 issues.
        assert_eq!(grouped.len(), 1);
        assert_eq!(grouped[0].0, "empty-repo");
        assert!(grouped[0].1.is_empty());
    }

    // ── Assignment::status_label ───────────────────────────────────────────────

    fn make_assignment(status: &str) -> Assignment {
        Assignment {
            id: "abc123def456".to_string(),
            repo: "test-repo".to_string(),
            issue_number: 1,
            issue_title: "Test issue".to_string(),
            machine: "testmachine".to_string(),
            status: status.to_string(),
            branch: None,
            model: None,
            dispatched_at: None,
            finished_at: None,
            exit_code: None,
            assignment_type: None,
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        }
    }

    #[test]
    fn status_label_running() {
        assert_eq!(make_assignment("running").status_label(), "RUN ");
    }

    #[test]
    fn status_label_done() {
        assert_eq!(make_assignment("done").status_label(), "DONE");
    }

    #[test]
    fn status_label_failed() {
        assert_eq!(make_assignment("failed").status_label(), "FAIL");
    }

    #[test]
    fn status_label_unknown_falls_back_to_pend() {
        assert_eq!(make_assignment("pending").status_label(), "PEND");
    }

    // ── json_str ──────────────────────────────────────────────────────────────

    #[test]
    fn json_str_simple_string_field() {
        let json = r#"{"type":"assistant","subtype":"init"}"#;
        assert_eq!(json_str(json, "type"), Some("assistant".to_string()));
        assert_eq!(json_str(json, "subtype"), Some("init".to_string()));
    }

    #[test]
    fn json_str_missing_field_returns_none() {
        let json = r#"{"type":"assistant"}"#;
        assert_eq!(json_str(json, "model"), None);
    }

    #[test]
    fn json_str_handles_backslash_n_escape() {
        let json = r#"{"text":"hello\nworld"}"#;
        // \n should become a space
        assert_eq!(json_str(json, "text"), Some("hello world".to_string()));
    }

    #[test]
    fn json_str_handles_escaped_quote() {
        let json = r#"{"text":"say \"hi\""}"#;
        assert_eq!(json_str(json, "text"), Some(r#"say "hi""#.to_string()));
    }

    // ── json_num ──────────────────────────────────────────────────────────────

    #[test]
    fn json_num_integer_field() {
        let json = r#"{"num_turns":42,"cost":0.5}"#;
        assert_eq!(json_num(json, "num_turns"), Some(42.0));
    }

    #[test]
    fn json_num_float_field() {
        let json = r#"{"total_cost_usd":1.23}"#;
        let v = json_num(json, "total_cost_usd").unwrap();
        assert!((v - 1.23).abs() < 1e-9);
    }

    #[test]
    fn json_num_missing_field_returns_none() {
        let json = r#"{"type":"result"}"#;
        assert_eq!(json_num(json, "num_turns"), None);
    }

    #[test]
    fn json_num_null_value_returns_none() {
        let json = r#"{"num_turns":null}"#;
        assert_eq!(json_num(json, "num_turns"), None);
    }

    // ── extract_tool_names ────────────────────────────────────────────────────

    #[test]
    fn extract_tool_names_finds_bash_in_assistant_content() {
        // Simplified assistant event with one tool_use block.
        let json = r#"{"type":"assistant","message":{"content":[{"type":"tool_use","id":"x","name":"Bash","input":{"command":"ls"}}]}}"#;
        let names = extract_tool_names(json);
        assert_eq!(names, vec!["Bash"]);
    }

    #[test]
    fn extract_tool_names_finds_multiple_unique_tools() {
        let json = r#"{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read"},{"type":"tool_use","name":"Edit"},{"type":"tool_use","name":"Read"}]}}"#;
        let names = extract_tool_names(json);
        // Deduped: Read and Edit only once each.
        assert_eq!(names.len(), 2);
        assert!(names.contains(&"Read".to_string()));
        assert!(names.contains(&"Edit".to_string()));
    }

    #[test]
    fn extract_tool_names_empty_if_no_tool_use() {
        let json = r#"{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}"#;
        assert!(extract_tool_names(json).is_empty());
    }

    #[test]
    fn extract_tool_names_handles_multibyte_glyph_at_window_boundary() {
        // Regression for the #218 watch-overlay crash: an Edit tool input
        // contained box-drawing glyphs (`─`, 3 UTF-8 bytes each) whose third
        // byte happened to land on the 200-byte slice boundary.
        // extract_tool_names took `&json[..200]` and panicked at the char
        // boundary. The fix rounds up to the next valid boundary.
        let prefix = r#"{"type":"tool_use","id":"x","name":"Edit","input":{"old_string":""#;
        // Pad with `─` repeated so byte 200 (relative to after the marker)
        // lands inside one of them.
        let payload: String = std::iter::repeat('─').take(80).collect();
        let suffix = r#""}}"#;
        let json = format!(r#"{{"type":"tool_use","irrelevant":"{}","#, "x".repeat(10))
            + prefix
            + &payload
            + suffix;
        // The inner tool_use should be found and its name extracted.
        let names = extract_tool_names(&json);
        assert!(names.contains(&"Edit".to_string()));
    }

    // ── extract_review_items ──────────────────────────────────────────────────

    #[test]
    fn extract_review_items_renders_verdict_and_body_lines() {
        // Result event with the structured REVIEW_VERDICT/REVIEW_BODY block
        // the way the reviewer system prompt asks workers to emit it. Body
        // text is JSON-encoded (`\n` → `\\n`).
        let line = r#"{"type":"result","result":"REVIEW_VERDICT: approve\nREVIEW_BODY:\n## Summary\n\nLGTM.\nEND_REVIEW"}"#;
        let items = extract_review_items(line);
        let texts: Vec<String> = items
            .iter()
            .map(|i| i.text.spans[0].text.clone())
            .collect();
        // First item is the verdict header.
        assert!(texts[0].contains("[review]"));
        assert!(texts[0].contains("approve"));
        // Subsequent items are the unescaped body lines.
        assert!(texts.iter().any(|t| t.contains("Summary")));
        assert!(texts.iter().any(|t| t.contains("LGTM.")));
    }

    #[test]
    fn extract_review_items_empty_when_no_verdict() {
        // Plain work-completion result event — no review block, no items.
        let line = r#"{"type":"result","result":"work done"}"#;
        let items = extract_review_items(line);
        assert!(items.is_empty());
    }

    // ── parse_json_event ──────────────────────────────────────────────────────

    #[test]
    fn parse_json_event_init_returns_item() {
        let json = r#"{"type":"system","subtype":"init","model":"claude-sonnet-4-6","session_id":"abc"}"#;
        let mut n = 0;
        let item = parse_json_event(json, &mut n);
        assert!(item.is_some());
        let text = &item.unwrap().text.spans[0].text;
        assert!(text.contains("[init]"));
        assert!(text.contains("claude-sonnet-4-6"));
    }

    #[test]
    fn parse_json_event_assistant_increments_turn_counter() {
        let json = r#"{"type":"assistant","message":{"content":[]}}"#;
        let mut n = 0usize;
        parse_json_event(json, &mut n);
        assert_eq!(n, 1);
        parse_json_event(json, &mut n);
        assert_eq!(n, 2);
    }

    #[test]
    fn parse_json_event_assistant_with_tool_shows_tool_name() {
        let json = r#"{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","id":"x"}]}}"#;
        let mut n = 0;
        let item = parse_json_event(json, &mut n).unwrap();
        let text = &item.text.spans[0].text;
        assert!(text.contains("tool_use=Bash"), "got: {}", text);
    }

    #[test]
    fn parse_json_event_status_in_text_block() {
        let json = r#"{"type":"assistant","message":{"content":[{"type":"text","text":"STATUS: did thing → doing next → confidence: high"}]}}"#;
        let mut n = 0;
        let item = parse_json_event(json, &mut n).unwrap();
        let text = &item.text.spans[0].text;
        assert!(text.starts_with("STATUS:"), "got: {}", text);
    }

    #[test]
    fn parse_json_event_stuck_in_text_block() {
        let json = r#"{"type":"assistant","message":{"content":[{"type":"text","text":"STUCK: tried X [why] [blocker]"}]}}"#;
        let mut n = 0;
        let item = parse_json_event(json, &mut n).unwrap();
        let text = &item.text.spans[0].text;
        assert!(text.starts_with("STUCK:"), "got: {}", text);
    }

    #[test]
    fn parse_json_event_result_shows_summary() {
        let json = r#"{"type":"result","num_turns":10,"total_cost_usd":0.42,"stop_reason":"end_turn","duration_ms":30000}"#;
        let mut n = 0;
        let item = parse_json_event(json, &mut n).unwrap();
        let text = &item.text.spans[0].text;
        assert!(text.contains("[result]"), "got: {}", text);
        assert!(text.contains("10 turns"), "got: {}", text);
    }

    #[test]
    fn parse_json_event_tool_result_returns_none() {
        // tool_result events are filtered out (too noisy).
        let json = r#"{"type":"tool_result","tool_use_id":"x","content":"ok"}"#;
        let mut n = 0;
        assert!(parse_json_event(json, &mut n).is_none());
    }

    // ── load_activity_log — plain text ────────────────────────────────────────

    #[test]
    fn load_activity_log_missing_file_returns_remote_notice() {
        let items = load_activity_log("nonexistent_assignment_id_xyz");
        assert_eq!(items.len(), 1);
        let text = &items[0].text.spans[0].text;
        assert!(text.contains("remote assignment"), "got: {}", text);
    }

    // ── Assignment::status_color ───────────────────────────────────────────────

    #[test]
    fn status_color_running() {
        assert_eq!(
            make_assignment("running").status_color(),
            Color::rgb(80, 220, 80)
        );
    }

    #[test]
    fn status_color_done() {
        assert_eq!(
            make_assignment("done").status_color(),
            Color::rgb(120, 120, 120)
        );
    }

    #[test]
    fn status_color_failed() {
        assert_eq!(
            make_assignment("failed").status_color(),
            Color::rgb(220, 70, 70)
        );
    }

    #[test]
    fn status_color_unknown_falls_back_to_yellow() {
        assert_eq!(
            make_assignment("pending").status_color(),
            Color::rgb(200, 200, 70)
        );
    }

    // ── Mouse helpers ─────────────────────────────────────────────────────────

    // The Board view no longer has a tab bar — all clicks in the Board main
    // panel return false (no-op) because there's nothing tab-switchable.

    #[test]
    fn mouse_main_click_board_always_returns_false() {
        let mut app = make_app_default();
        let main_b = Rect::new(50.0, 0.0, 40.0, 40.0);
        // #249 Principle 1: the Board panel now has a toolbar row at the
        // top (`tb_h = lh * 1.4 = 1.4` with lh=1.0).  Click well below
        // that — the Board body itself has no clickable widgets so the
        // click still returns false.
        let changed = app.mouse_main_click(Point::new(51.0, 10.0), main_b, 1.0);
        assert!(!changed);
    }

    // ── #269: tab-click hit-test from labels ────────────────────────────

    #[test]
    fn hit_tab_index_from_labels_resolves_first_tab() {
        let labels = [" Board ", " Issue "];
        // Click at x=3 falls inside " Board " (chars 0-6).
        assert_eq!(hit_tab_index_from_labels(&labels, 0.0, 3.0), Some(0));
    }

    #[test]
    fn hit_tab_index_from_labels_resolves_second_tab() {
        let labels = [" Board ", " Issue "];
        // " Board " is 7 chars wide, " Issue " covers x=7..14.
        assert_eq!(hit_tab_index_from_labels(&labels, 0.0, 10.0), Some(1));
    }

    #[test]
    fn hit_tab_index_from_labels_resolves_third_tab_with_origin_offset() {
        // Origin shifted to x=50 (sidebar panel offset).  Labels still
        // sum left-to-right from the origin.
        let labels = [" Pipeline ", " Issue ", " Stages "];
        // " Pipeline " is 10 chars → " Issue " starts at 60.
        // " Issue " is 7 chars → " Stages " starts at 67.
        // Click at x=70 should land in " Stages ".
        assert_eq!(hit_tab_index_from_labels(&labels, 50.0, 70.0), Some(2));
    }

    #[test]
    fn hit_tab_index_from_labels_returns_none_past_last_tab() {
        let labels = [" Board ", " Issue "];
        assert_eq!(hit_tab_index_from_labels(&labels, 0.0, 100.0), None);
    }

    #[test]
    fn hit_tab_index_from_labels_robust_to_label_growth() {
        // If a tab label gains a badge (e.g. " Stages (3) "), the new
        // width is honoured automatically — no hard-coded constant
        // breaks here.
        let labels = [" Pipeline ", " Issue ", " Stages (3) "];
        // " Pipeline " (10) + " Issue " (7) = 17 → " Stages (3) " starts at 17.
        assert_eq!(hit_tab_index_from_labels(&labels, 0.0, 20.0), Some(2));
    }

    #[test]
    fn mouse_main_click_below_tab_row_is_ignored() {
        let mut app = make_app_default();
        let main_b = Rect::new(50.0, 0.0, 40.0, 40.0);
        // Click well below the toolbar (`tb_h = 1.4`).
        let changed = app.mouse_main_click(Point::new(55.0, 10.0), main_b, 1.0);
        assert!(!changed);
    }

    #[test]
    fn mouse_main_click_non_board_view_is_ignored() {
        let mut app = make_app_default();
        app.active_view = SidebarView::Machines;
        let main_b = Rect::new(50.0, 0.0, 40.0, 40.0);
        let changed = app.mouse_main_click(Point::new(55.0, 0.0), main_b, 1.0);
        assert!(!changed);
    }

    // #201: clicking a Pipeline stage's action button must dispatch.
    // The wiring goes through PipelineHit::Action(stage_idx) →
    // dispatch_pipeline_stage(idx). This test verifies the click reaches
    // the dispatcher; an integration test of dispatch itself lives elsewhere.
    #[test]
    fn mouse_click_on_pipeline_stage_action_dispatches() {
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_detail_tab = PipelineDetailTab::Pipeline;
        app.pipeline_sel = Some(0);
        // No assignments → Work stage is Pending with no predecessors, so it
        // gets a [Go] action button per build_pipeline_widget.

        // Use a large content rect so the layout produces well-separated stages.
        let main_b = Rect::new(0.0, 0.0, 200.0, 40.0);
        let lh: f32 = 1.0;
        // #272: panel toolbar is now 2 cells tall (was 1.4) since the
        // quadraui rasteriser paints multi-row toolbars.
        let tb_h = lh * 2.0;
        let tab_h = lh * 1.4; // pipeline detail tab bar below toolbar
        let after_toolbar = Rect::new(
            main_b.x,
            main_b.y + tb_h,
            main_b.width,
            (main_b.height - tb_h).max(0.0),
        );
        let content_rect = Rect::new(
            after_toolbar.x,
            after_toolbar.y + tab_h,
            after_toolbar.width,
            (after_toolbar.height - tab_h).max(0.0),
        );
        let pv_rect = pipeline_detail_pv_rect(content_rect, lh);
        let view = app.build_pipeline_widget().expect("widget");
        let layout = tui_pipeline_layout(&view, pv_rect);

        // Work is stage index 0 and should carry the [Go] action.
        let work_stage = &layout.stages[0];
        let ab = work_stage
            .action_bounds
            .expect("Work stage should have a [Go] action button");
        let click_pos = Point::new(ab.x + ab.width / 2.0, ab.y + ab.height / 2.0);

        // mouse_main_click returns true if state changed (dispatch attempted).
        // The dispatch may toast an error if no machine is reachable in tests,
        // but the wiring itself is what we're verifying — the click reaches
        // dispatch_pipeline_stage, which is the integration the user lost in
        // a prior session.
        let result = app.mouse_main_click(click_pos, main_b, lh);
        assert!(result, "click on stage action button should be handled");
    }

    // ── Scroll offset preservation across refresh ─────────────────────────────

    #[test]
    fn detail_scroll_preserved_after_refresh_on_fixed_data() {
        let mut app = make_app_default();
        // Set a non-zero detail scroll offset.
        app.detail_scroll = 3;
        // `refresh()` re-reads the DB; with no DB present it loads empty data.
        // The scroll offset must not be clamped to zero by refresh().
        app.refresh();
        assert_eq!(app.detail_scroll, 3);
    }

    #[test]
    fn machine_detail_scroll_preserved_after_refresh() {
        let mut app = make_app_default();
        app.machine_detail_scroll = 5;
        app.refresh();
        assert_eq!(app.machine_detail_scroll, 5);
    }

    // ── Board sidebar state preservation across rebuild ───────────────────────

    #[test]
    fn rebuild_board_sidebar_preserves_panel_scroll() {
        let assignments = vec![
            make_assignment_typed("running", 10, "repo-a", Some("work")),
            make_assignment_typed("done", 20, "repo-b", Some("work")),
        ];
        let mut app = make_app_with_assignments(assignments);
        // Simulate the user having scrolled the sidebar panel.
        app.board_sidebar.set_panel_scroll(42.0);
        // A 5-second data refresh triggers rebuild_board_sidebar().
        app.rebuild_board_sidebar();
        assert_eq!(
            app.board_sidebar.panel_scroll(),
            42.0,
            "panel_scroll should survive rebuild"
        );
    }

    #[test]
    fn rebuild_board_sidebar_preserves_collapsed_state() {
        let assignments = vec![
            make_assignment_typed("running", 10, "repo-a", Some("work")),
            make_assignment_typed("done", 20, "repo-b", Some("work")),
        ];
        let mut app = make_app_with_assignments(assignments);
        // Section 0 = search form; section 1 = repo-a; section 2 = repo-b.
        app.board_sidebar.set_collapsed(1, true);
        assert!(app.board_sidebar.is_collapsed(1));
        app.rebuild_board_sidebar();
        assert!(
            app.board_sidebar.is_collapsed(1),
            "collapsed state should survive rebuild"
        );
        assert!(
            !app.board_sidebar.is_collapsed(2),
            "uncollapsed section should remain uncollapsed"
        );
    }

    #[test]
    fn rebuild_board_sidebar_new_repo_auto_collapses_when_empty() {
        // On the first build (no previous state) an empty repo is auto-collapsed.
        let mut app = make_test_app(BoardData {
            machines: vec![Machine {
                name: "m1".to_string(),
                host: String::new(),
                reachable: true,
                active_count: 0,
                repos: vec!["empty-repo".to_string()],
            }],
            ..BoardData::default()
        });
        app.rebuild_board_sidebar();
        // Section 0 = search form; section 1 = empty-repo.
        assert!(
            app.board_sidebar.is_collapsed(1),
            "empty repo should be auto-collapsed on first build"
        );
    }

    #[test]
    fn rebuild_board_sidebar_preserves_selection_across_rebuild() {
        let assignments = vec![
            make_assignment_typed("running", 10, "repo-a", Some("work")),
            make_assignment_typed("done", 20, "repo-b", Some("work")),
        ];
        let mut app = make_app_with_assignments(assignments);
        // Select issue #20 in repo-b (section 1, row 0).
        app.select_issue("repo-b", 20);
        let before = app.board_selected_issue();
        app.rebuild_board_sidebar();
        let after = app.board_selected_issue();
        assert_eq!(before, after, "selection should survive rebuild");
    }

    // ── Board status grouping and fuzzy search ────────────────────────────

    #[test]
    fn board_search_section_is_section_zero() {
        let mut app = make_app_default();
        app.rebuild_board_sidebar();
        assert!(app.board_sidebar.form(0).is_some(), "section 0 should be the search form");
    }

    #[test]
    fn board_repos_start_at_section_one_without_proposals() {
        let assignments = vec![make_assignment_typed("running", 10, "repo-a", Some("work"))];
        let app = make_app_with_assignments(assignments);
        assert!(!app.has_proposals_section);
        assert_eq!(app.board_repo_offset(), 1);
    }

    #[test]
    fn board_fuzzy_search_hides_non_matching_issues() {
        let assignments = vec![
            make_assignment_typed("running", 42, "repo-a", Some("work")),
            make_assignment_typed("done", 99, "repo-a", Some("work")),
        ];
        let mut app = make_app_with_assignments(assignments);
        app.board_search = "42".to_string();
        app.board_search_cursor = 2;
        app.rebuild_board_sidebar();
        let cache = app.board_issues_cache.clone();
        let groups = app.board_grouped_for_repo(&cache, "repo-a");
        assert_eq!(groups.len(), 1, "only Running group should be visible");
        assert_eq!(groups[0].1.len(), 1, "only issue #42 should match");
        assert_eq!(groups[0].1[0].1.issue_number, 42);
    }

    /// #257 fix helper: ensure every test issue has an `open` row in the
    /// open_issues cache.  Without this, `lifecycle_section` treats a
    /// settled (done/merged) assignment as Completed because the brain
    /// has no record of the issue being open.
    fn seed_open_issue_records(app: &mut CoordApp, repo: &str, numbers: &[u64]) {
        for &n in numbers {
            app.data.open_issues.push(OpenIssue {
                repo_name: repo.to_string(),
                number: n,
                title: format!("issue #{n}"),
                body: String::new(),
                state: "open".to_string(),
                labels: Vec::new(),
            });
        }
        app.rebuild_board_sidebar();
    }

    #[test]
    fn board_lifecycle_sections_in_flight_when_open_with_assignments() {
        // #256 lifecycle model: open issues with assignments fold into
        // a single In-flight bucket — running / failed / done all land
        // together.  Requires the brain to have the issue cached as
        // open (otherwise done assignments route to Completed; see the
        // `stale_done_with_no_cache_record_routes_to_completed` test).
        let assignments = vec![
            make_assignment_typed("done", 1, "repo-a", Some("work")),
            make_assignment_typed("running", 2, "repo-a", Some("work")),
            make_assignment_typed("failed", 3, "repo-a", Some("work")),
        ];
        let mut app = make_app_with_assignments(assignments);
        seed_open_issue_records(&mut app, "repo-a", &[1, 2, 3]);
        let cache = app.board_issues_cache.clone();
        let groups = app.board_grouped_for_repo(&cache, "repo-a");
        assert_eq!(groups.len(), 1);
        assert_eq!(groups[0].0, "in-flight");
        assert_eq!(
            groups[0].1.len(),
            3,
            "all three open+assigned issues fold into In-flight",
        );
    }

    #[test]
    fn stale_done_with_no_cache_record_routes_to_completed() {
        // #257 fix: a done assignment whose issue isn't in the brain's
        // cache (typical of historical work — the brain prunes closed
        // rows after 7d) must NOT pollute In-flight.  Route to
        // Completed instead.
        let app = make_app_with_assignments(vec![make_assignment_typed(
            "done", 42, "repo-a", Some("work"),
        )]);
        // No open_issues entry → has_open_record is false.
        let cache = app.board_issues_cache.clone();
        let groups = app.board_grouped_for_repo(&cache, "repo-a");
        assert_eq!(groups.len(), 1);
        assert_eq!(
            groups[0].0, "completed",
            "stale done assignment with no cache record belongs in Completed, not In-flight",
        );
    }

    #[test]
    fn stale_running_with_no_cache_record_stays_in_flight() {
        // A running assignment with no cache record is most likely a
        // freshly-dispatched issue the brain hasn't synced yet — must
        // STAY in In-flight so the user can see it working.
        let app = make_app_with_assignments(vec![make_assignment_typed(
            "running", 99, "repo-a", Some("work"),
        )]);
        let cache = app.board_issues_cache.clone();
        let groups = app.board_grouped_for_repo(&cache, "repo-a");
        assert_eq!(groups[0].0, "in-flight");
    }

    #[test]
    fn merged_assignment_routes_to_completed_regardless_of_cache() {
        // status_summary == "merged" implies the PR closed the issue
        // via `fixes #N` even before the brain has synced the close.
        let mut app = make_app_with_assignments(vec![make_assignment_typed(
            "done", 7, "repo-a", Some("work"),
        )]);
        // Cache says open, but the merge_queue marks the work merged
        // → status_summary becomes "merged" → Completed.
        app.data.merge_queue.push(MergeQueueEntry {
            assignment_id: "id-7-done".to_string(),
            issue_number: Some(7),
            state: "merged".to_string(),
            pr_number: Some(1),
            pr_url: None,
            repo_github: "acme/repo-a".to_string(),
        });
        seed_open_issue_records(&mut app, "repo-a", &[7]);
        let cache = app.board_issues_cache.clone();
        let groups = app.board_grouped_for_repo(&cache, "repo-a");
        assert_eq!(groups.len(), 1);
        assert_eq!(groups[0].0, "completed");
    }

    #[test]
    fn board_lifecycle_splits_pending_into_backlog_refining_refined() {
        // #226: open issues without assignments split into three
        // sections by their `status:*` label set.
        let mut app = make_app_default();
        // No labels → Backlog
        app.data.open_issues.push(OpenIssue {
            repo_name: "repo-a".to_string(),
            number: 1,
            title: "raw issue".to_string(),
            body: String::new(),
            state: "open".to_string(),
            labels: Vec::new(),
        });
        // status:refining → Refining
        app.data.open_issues.push(OpenIssue {
            repo_name: "repo-a".to_string(),
            number: 2,
            title: "in scoping".to_string(),
            body: String::new(),
            state: "open".to_string(),
            labels: vec!["status:refining".to_string()],
        });
        // status:ready → Refined
        app.data.open_issues.push(OpenIssue {
            repo_name: "repo-a".to_string(),
            number: 3,
            title: "ready".to_string(),
            body: String::new(),
            state: "open".to_string(),
            labels: vec!["status:ready".to_string()],
        });
        app.rebuild_board_sidebar();
        let cache = app.board_issues_cache.clone();
        let groups = app.board_grouped_for_repo(&cache, "repo-a");
        let by_key: std::collections::HashMap<&str, usize> = groups
            .iter()
            .map(|(k, v)| (*k, v.len()))
            .collect();
        assert_eq!(by_key.get("backlog"), Some(&1), "Backlog count");
        assert_eq!(by_key.get("refining"), Some(&1), "Refining count");
        assert_eq!(by_key.get("refined"), Some(&1), "Refined count");
    }

    #[test]
    fn board_lifecycle_sections_in_display_order() {
        // #226: section ordering is Backlog → Refining → Refined →
        // In-flight → Completed.
        let mut app = make_app_default();
        // One issue per section that has rows (Backlog, Refining,
        // Refined, In-flight, Completed).
        app.data.open_issues.push(OpenIssue {
            repo_name: "repo-a".to_string(), number: 1,
            title: "b".to_string(), body: String::new(),
            state: "open".to_string(), labels: Vec::new(),
        });
        app.data.open_issues.push(OpenIssue {
            repo_name: "repo-a".to_string(), number: 2,
            title: "r".to_string(), body: String::new(),
            state: "open".to_string(),
            labels: vec!["status:refining".to_string()],
        });
        app.data.open_issues.push(OpenIssue {
            repo_name: "repo-a".to_string(), number: 3,
            title: "rd".to_string(), body: String::new(),
            state: "open".to_string(),
            labels: vec!["status:ready".to_string()],
        });
        // Issue 4: in-flight (open issue with running assignment).
        app.data.assignments.push(make_assignment_typed(
            "running", 4, "repo-a", Some("work"),
        ));
        app.data.open_issues.push(OpenIssue {
            repo_name: "repo-a".to_string(), number: 4,
            title: "if".to_string(), body: String::new(),
            state: "open".to_string(), labels: Vec::new(),
        });
        // Issue 5: completed (closed issue with done assignment).
        app.data.assignments.push(make_assignment_typed(
            "done", 5, "repo-a", Some("work"),
        ));
        app.data.open_issues.push(OpenIssue {
            repo_name: "repo-a".to_string(), number: 5,
            title: "c".to_string(), body: String::new(),
            state: "closed".to_string(), labels: Vec::new(),
        });
        app.rebuild_board_sidebar();
        let cache = app.board_issues_cache.clone();
        let groups = app.board_grouped_for_repo(&cache, "repo-a");
        let order: Vec<&str> = groups.iter().map(|(k, _)| *k).collect();
        assert_eq!(
            order,
            vec!["backlog", "refining", "refined", "in-flight", "completed"],
        );
    }

    #[test]
    fn board_lifecycle_completed_for_closed_issues_with_assignments() {
        // #265: an assignment whose issue is closed on GitHub lands in
        // Completed, not In-flight, regardless of the assignment's own
        // status_summary.
        let mut app = make_app_with_assignments(vec![make_assignment_typed(
            "done", 10, "repo-a", Some("work"),
        )]);
        // Mark issue #10 as closed in the open_issues cache.
        app.data.open_issues.push(OpenIssue {
            repo_name: "repo-a".to_string(),
            number: 10,
            title: "closed one".to_string(),
            body: String::new(),
            state: "closed".to_string(),
            labels: Vec::new(),
        });
        app.rebuild_board_sidebar();
        let cache = app.board_issues_cache.clone();
        let groups = app.board_grouped_for_repo(&cache, "repo-a");
        assert_eq!(groups.len(), 1);
        assert_eq!(groups[0].0, "completed");
    }

    #[test]
    fn board_empty_lifecycle_sections_are_hidden() {
        // A repo with a single running assignment shows only the
        // In-flight group; empty Pending / Completed buckets stay out
        // of the sidebar.
        let assignments = vec![make_assignment_typed("running", 10, "repo-a", Some("work"))];
        let app = make_app_with_assignments(assignments);
        let cache = app.board_issues_cache.clone();
        let groups = app.board_grouped_for_repo(&cache, "repo-a");
        assert_eq!(groups.len(), 1);
        assert_eq!(groups[0].0, "in-flight");
    }

    #[test]
    fn board_select_issue_uses_two_level_path() {
        // Two open issues with assignments → both in the single
        // In-flight group.  Path [0, 1] selects the second issue.
        let assignments = vec![
            make_assignment_typed("running", 10, "repo-a", Some("work")),
            make_assignment_typed("done", 20, "repo-a", Some("work")),
        ];
        let mut app = make_app_with_assignments(assignments);
        // Seed open cache records so the `done` assignment doesn't
        // route to Completed.
        seed_open_issue_records(&mut app, "repo-a", &[10, 20]);
        app.select_issue("repo-a", 20);
        let path = app.board_sidebar.selected_path(1).cloned();
        // The flat order inside In-flight is the sorted-by-issue-number
        // order from `issues_by_repo`; #20 sits at index 1.
        assert!(
            path == Some(vec![0u16, 1u16]) || path == Some(vec![0u16, 0u16]),
            "selected issue must land inside the single In-flight group, got {:?}",
            path,
        );
        let sel = app.board_selected_issue();
        assert_eq!(sel, Some(("repo-a".to_string(), 20)));
    }

    // ── Pipeline panel ────────────────────────────────────────────────────

    fn make_pipeline_app() -> CoordApp {
        let data = BoardData {
            pipeline_default_gates: vec!["review".to_string(), "merge".to_string()],
            pipeline_tracked_labels: vec!["coord".to_string()],
            pipeline_repos: vec![("api".to_string(), "acme/api".to_string())],
            machines: vec![Machine {
                name: "m1".to_string(),
                host: String::new(),
                reachable: true,
                active_count: 0,
                repos: vec!["api".to_string()],
            }],
            ..BoardData::default()
        };
        let mut app = make_test_app(data);
        // #225: Pipeline only shows New / In-progress / Done.  Fixture
        // issues carry `status:ready` so they classify as Pipeline:New
        // (the umbrella's name; classifier key is still legacy
        // `"pending"`) and stay visible in the sidebar.
        app.pipeline_issues = vec![
            PipelineIssue {
                number: 42,
                title: "Add cool thing".to_string(),
                body: String::new(),
                repo_slug: "acme/api".to_string(),
                coord_repo: Some("api".to_string()),
                matched_labels: vec!["coord".to_string()],
                all_labels: vec!["coord".to_string(), "status:ready".to_string()],
                is_closed: false,
            },
            PipelineIssue {
                number: 99,
                title: "Mystery repo issue".to_string(),
                body: String::new(),
                repo_slug: "other/repo".to_string(),
                coord_repo: None,
                matched_labels: vec!["coord".to_string()],
                all_labels: vec!["coord".to_string(), "status:ready".to_string()],
                is_closed: false,
            },
        ];
        app.rebuild_pipeline_sidebar(None);
        app
    }

    #[test]
    fn pipeline_stage_names_prepends_work() {
        let app = make_pipeline_app();
        assert_eq!(
            app.pipeline_stage_names(),
            vec!["work".to_string(), "review".to_string(), "merge".to_string()]
        );
    }

    #[test]
    fn pipeline_stage_names_dedupes_explicit_work_in_gates() {
        let mut app = make_pipeline_app();
        app.data.pipeline_default_gates = vec![
            "work".to_string(),
            "review".to_string(),
        ];
        // Explicit "work" in default_gates must not duplicate the prepended one.
        assert_eq!(
            app.pipeline_stage_names(),
            vec!["work".to_string(), "review".to_string()]
        );
    }

    #[test]
    fn pipeline_stage_names_for_issue_prepends_plan_when_plan_assignment_exists() {
        // The motivating bug: user clicks right-click → Start with Plan,
        // a plan-typed assignment runs, but the stage strip has no
        // "plan" stage because `require_plan` is false.  The
        // per-issue helper must prepend "plan" so the user sees the
        // plan's progress as its own stage.
        let mut app = make_pipeline_app();
        assert!(!app.data.pipeline_require_plan);
        // Issue #42 from the fixture; tag a plan-typed assignment for it.
        let mut plan = _stage_assignment("p1", "plan", 100.0, "done");
        plan.issue_number = 42;
        plan.repo = "api".to_string();
        app.data.assignments.push(plan);
        let issue = app.pipeline_issues[0].clone();
        let stages = app.pipeline_stage_names_for_issue(&issue);
        assert_eq!(stages.first(), Some(&"plan".to_string()));
    }

    #[test]
    fn pipeline_stage_names_for_issue_omits_plan_when_no_plan_assignment() {
        // No plan-typed assignment, no require_plan → no Plan stage.
        // Preserves the original behaviour for projects that don't use
        // Plan workers.
        let app = make_pipeline_app();
        let issue = app.pipeline_issues[0].clone();
        let stages = app.pipeline_stage_names_for_issue(&issue);
        assert!(!stages.contains(&"plan".to_string()));
    }

    #[test]
    fn assignments_for_stage_keeps_plan_in_plan_when_plan_stage_exists() {
        // With a per-issue Plan stage present, plan-typed assignments
        // must surface under "plan" — NOT folded into "work".  Without
        // this fix the Work stage went green on a plan-only dispatch.
        let mut app = make_pipeline_app();
        let mut plan = _stage_assignment("p1", "plan", 100.0, "done");
        plan.issue_number = 42;
        plan.repo = "api".to_string();
        app.data.assignments.push(plan);
        let issue = app.pipeline_issues[0].clone();
        // Work stage has no assignments (plan one belongs to Plan).
        let work = app.assignments_for_stage(&issue, "work");
        assert!(work.is_empty(), "plan must not fold into Work when Plan stage exists");
        // Plan stage owns the plan-typed assignment.
        let plan_stage = app.assignments_for_stage(&issue, "plan");
        assert_eq!(plan_stage.len(), 1);
    }

    #[test]
    fn rebuild_pipeline_sidebar_groups_issues_by_repo() {
        let app = make_pipeline_app();
        // Two issues from two different repos → two repo sections.
        assert_eq!(app.pipeline_repo_names.len(), 2);
        assert_eq!(app.pipeline_issues.len(), 2);
        // First repo key comes from coord_repo ("api") for issue #42.
        assert_eq!(app.pipeline_repo_names[0], "api");
        // Second repo has no coord mapping, falls back to repo_slug.
        assert_eq!(app.pipeline_repo_names[1], "other/repo");
    }

    #[test]
    fn rebuild_pipeline_sidebar_default_selects_first_issue() {
        let app = make_pipeline_app();
        // Default selection should resolve to the first issue (index 0).
        assert!(app.pipeline_sel.is_some());
        assert_eq!(app.pipeline_sel.unwrap(), 0);
    }

    #[test]
    fn pipeline_groups_for_repo_filters_out_pre_pipeline_sections() {
        // #225: Pipeline shows only New (status:ready+no assignments),
        // In-progress (has assignments), and Done (closed+assignments).
        // Unlabelled (`new`) and `status:refining` rows belong on the
        // Board, not the Pipeline.
        let mut app = make_pipeline_app();
        // Issue #42 has status:ready → New (visible).
        // Add issue #43 with no labels → "new" classifier → HIDDEN.
        app.pipeline_issues.push(PipelineIssue {
            number: 43,
            title: "Backlog".to_string(),
            body: String::new(),
            repo_slug: "acme/api".to_string(),
            coord_repo: Some("api".to_string()),
            matched_labels: vec!["coord".to_string()],
            all_labels: vec!["coord".to_string()],
            is_closed: false,
        });
        // Issue #44 with status:refining → "refining" classifier → HIDDEN.
        app.pipeline_issues.push(PipelineIssue {
            number: 44,
            title: "Refining".to_string(),
            body: String::new(),
            repo_slug: "acme/api".to_string(),
            coord_repo: Some("api".to_string()),
            matched_labels: vec!["coord".to_string()],
            all_labels: vec!["coord".to_string(), "status:refining".to_string()],
            is_closed: false,
        });

        let groups = app.pipeline_groups_for_repo("api");
        // Only the `pending` (= New) section visible — #43 and #44 hidden.
        assert_eq!(groups.len(), 1);
        assert_eq!(groups[0].0, "pending");
        // Issue #42 is the only one in the New section.
        assert_eq!(groups[0].1.len(), 1);
        let visible_numbers: Vec<u64> = groups[0]
            .1
            .iter()
            .map(|i| app.pipeline_issues[*i].number)
            .collect();
        assert_eq!(visible_numbers, vec![42]);
    }

    #[test]
    fn pipeline_groups_for_repo_visible_section_order() {
        // #225 / #256: visible sections render in the order New →
        // In-progress → Done.  Set up one of each.
        let mut app = make_pipeline_app();
        // #42 already has status:ready → New.
        // Add #43 with status:ready + an assignment → In-progress.
        app.pipeline_issues.push(PipelineIssue {
            number: 43,
            title: "Active".to_string(),
            body: String::new(),
            repo_slug: "acme/api".to_string(),
            coord_repo: Some("api".to_string()),
            matched_labels: vec!["coord".to_string()],
            all_labels: vec!["coord".to_string(), "status:ready".to_string()],
            is_closed: false,
        });
        app.data.assignments.push(make_assignment_typed(
            "running", 43, "api", Some("work"),
        ));
        // Add #44 as closed with an assignment → Done.
        app.pipeline_issues.push(PipelineIssue {
            number: 44,
            title: "Finished".to_string(),
            body: String::new(),
            repo_slug: "acme/api".to_string(),
            coord_repo: Some("api".to_string()),
            matched_labels: vec!["coord".to_string()],
            all_labels: vec!["coord".to_string(), "status:ready".to_string()],
            is_closed: true,
        });
        app.data.assignments.push(make_assignment_typed(
            "done", 44, "api", Some("work"),
        ));

        let groups = app.pipeline_groups_for_repo("api");
        let order: Vec<&str> = groups.iter().map(|(k, _)| *k).collect();
        assert_eq!(order, vec!["pending", "in-progress", "done"]);
    }

    #[test]
    fn rebuild_pipeline_sidebar_lifecycle_new_when_no_labels() {
        let mut app = make_pipeline_app();
        // #225 fixture seeds `status:ready` — strip it for this test
        // so the classifier sees an unlabelled issue and returns "new".
        app.pipeline_issues[0]
            .all_labels
            .retain(|l| !l.starts_with("status:"));
        let section = app.pipeline_lifecycle_section(&app.pipeline_issues[0]);
        assert_eq!(section, "new");
    }

    #[test]
    fn rebuild_pipeline_sidebar_lifecycle_refining() {
        let mut app = make_pipeline_app();
        // #225 fixture seeds `status:ready` — replace with refining.
        app.pipeline_issues[0]
            .all_labels
            .retain(|l| !l.starts_with("status:"));
        app.pipeline_issues[0].all_labels.push("status:refining".to_string());
        let section = app.pipeline_lifecycle_section(&app.pipeline_issues[0]);
        assert_eq!(section, "refining");
    }

    #[test]
    fn rebuild_pipeline_sidebar_lifecycle_pending() {
        let mut app = make_pipeline_app();
        app.pipeline_issues[0].all_labels.push("status:ready".to_string());
        let section = app.pipeline_lifecycle_section(&app.pipeline_issues[0]);
        assert_eq!(section, "pending");
    }

    #[test]
    fn rebuild_pipeline_sidebar_lifecycle_in_progress_beats_ready_label() {
        let mut app = make_pipeline_app();
        app.pipeline_issues[0].all_labels.push("status:ready".to_string());
        app.data.assignments.push(Assignment {
            id: "x1".to_string(),
            repo: "api".to_string(),
            issue_number: 42,
            issue_title: "Add cool thing".to_string(),
            machine: "m1".to_string(),
            status: "running".to_string(),
            branch: None,
            model: None,
            dispatched_at: Some(1.0),
            finished_at: None,
            exit_code: None,
            assignment_type: Some("work".to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        });
        // Has assignment → in-progress, even though status:ready label is set.
        let section = app.pipeline_lifecycle_section(&app.pipeline_issues[0]);
        assert_eq!(section, "in-progress");
    }

    #[test]
    fn rebuild_pipeline_sidebar_lifecycle_done_when_closed() {
        let mut app = make_pipeline_app();
        app.pipeline_issues[0].is_closed = true;
        let section = app.pipeline_lifecycle_section(&app.pipeline_issues[0]);
        assert_eq!(section, "done");
    }

    #[test]
    fn rebuild_pipeline_sidebar_lifecycle_done_beats_in_progress() {
        let mut app = make_pipeline_app();
        app.pipeline_issues[0].is_closed = true;
        app.data.assignments.push(Assignment {
            id: "x2".to_string(),
            repo: "api".to_string(),
            issue_number: 42,
            issue_title: "Add cool thing".to_string(),
            machine: "m1".to_string(),
            status: "running".to_string(),
            branch: None,
            model: None,
            dispatched_at: Some(1.0),
            finished_at: None,
            exit_code: None,
            assignment_type: Some("work".to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        });
        // is_closed wins over has-assignment.
        let section = app.pipeline_lifecycle_section(&app.pipeline_issues[0]);
        assert_eq!(section, "done");
    }

    // ── Pipeline selection preservation across pipeline_issues swaps ──────

    /// The canonical regression test for "the 15-second refresh keeps
    /// jumping me back to the top".  Reproduces the poll_pipeline_loader
    /// pattern: capture the selection identity, swap pipeline_issues,
    /// rebuild with the captured identity.
    #[test]
    fn rebuild_pipeline_sidebar_preserves_selection_when_prev_sel_captured_before_swap() {
        let mut app = make_pipeline_app();
        // Default selection lands on #42 (api).
        assert_eq!(app.pipeline_sel, Some(0));
        assert_eq!(app.pipeline_issues[0].number, 42);

        // Capture BEFORE the swap (the fix).
        let prev_sel = app.capture_pipeline_selection_id();
        assert_eq!(prev_sel, Some(("acme/api".to_string(), 42)));

        // Simulate a refresh that prepends a new issue, shifting #42 to index 1.
        let mut new_issues = vec![PipelineIssue {
            number: 7,
            title: "New backlog item".to_string(),
            body: String::new(),
            repo_slug: "acme/api".to_string(),
            coord_repo: Some("api".to_string()),
            matched_labels: vec!["coord".to_string()],
            all_labels: vec!["coord".to_string()],
            is_closed: false,
        }];
        new_issues.extend(app.pipeline_issues.clone());
        app.pipeline_issues = new_issues;

        app.rebuild_pipeline_sidebar(prev_sel);

        let selected_number = app
            .pipeline_sel
            .and_then(|i| app.pipeline_issues.get(i))
            .map(|i| i.number);
        assert_eq!(
            selected_number,
            Some(42),
            "selection must follow #42 even though its index moved from 0 to 1",
        );
    }

    /// Documents the BUG path: callers that swap pipeline_issues without
    /// capturing prev_sel first end up restoring the wrong issue.  Locks
    /// in the expectation so a future regression in poll_pipeline_loader
    /// (e.g. someone removing the capture call) shows up as a failing
    /// test rather than a silent selection-jump.
    #[test]
    fn rebuild_pipeline_sidebar_without_prev_sel_reads_stale_index() {
        let mut app = make_pipeline_app();
        assert_eq!(app.pipeline_sel, Some(0));
        assert_eq!(app.pipeline_issues[0].number, 42);

        // Swap WITHOUT capturing prev_sel first — the buggy pattern.
        // #225: include `status:ready` so the new issue lands in the
        // visible Pipeline:New (formerly "pending") section.
        let mut new_issues = vec![PipelineIssue {
            number: 7,
            title: "New backlog item".to_string(),
            body: String::new(),
            repo_slug: "acme/api".to_string(),
            coord_repo: Some("api".to_string()),
            matched_labels: vec!["coord".to_string()],
            all_labels: vec!["coord".to_string(), "status:ready".to_string()],
            is_closed: false,
        }];
        new_issues.extend(app.pipeline_issues.clone());
        app.pipeline_issues = new_issues;

        // No override → internal fallback reads pipeline_sel(=Some(0))
        // against the new list → captures #7 (not #42) → restores #7.
        app.rebuild_pipeline_sidebar(None);

        let selected_number = app
            .pipeline_sel
            .and_then(|i| app.pipeline_issues.get(i))
            .map(|i| i.number);
        assert_eq!(
            selected_number,
            Some(7),
            "documents the bug: stale index reads the wrong issue from the new list",
        );
    }

    #[test]
    fn capture_pipeline_selection_id_returns_none_when_no_selection() {
        let mut app = make_pipeline_app();
        app.pipeline_sel = None;
        assert_eq!(app.capture_pipeline_selection_id(), None);
    }

    #[test]
    fn capture_pipeline_selection_id_returns_repo_and_number() {
        let mut app = make_pipeline_app();
        app.pipeline_sel = Some(1); // #99 / other/repo
        assert_eq!(
            app.capture_pipeline_selection_id(),
            Some(("other/repo".to_string(), 99)),
        );
    }

    /// The 15s pipeline refresh used to reset the sidebar's panel scroll
    /// to 0, which yanked the visible area to the top even when the
    /// selection itself was restored.  Test that the scroll offset is
    /// preserved across a rebuild_pipeline_sidebar call.
    #[test]
    fn rebuild_pipeline_sidebar_preserves_panel_scroll() {
        let mut app = make_pipeline_app();
        // Set a non-zero panel scroll (simulating the user having scrolled).
        app.pipeline_sidebar.set_panel_scroll(42.0);
        assert_eq!(app.pipeline_sidebar.panel_scroll(), 42.0);

        // Simulate a refresh that doesn't change the issue list.
        app.rebuild_pipeline_sidebar(None);

        assert_eq!(
            app.pipeline_sidebar.panel_scroll(),
            42.0,
            "panel scroll must survive rebuild — otherwise the view yanks to top on refresh",
        );
    }

    /// Section collapse state must survive a rebuild — without this, the
    /// user collapses the large claude-coordinator section and watches it
    /// re-expand on every 15 s refresh.
    #[test]
    fn rebuild_pipeline_sidebar_preserves_section_collapsed_state() {
        let mut app = make_pipeline_app();
        // make_pipeline_app has two sections: "api" (index 0) and "other/repo"
        // (index 1). Collapse the first one.
        app.pipeline_sidebar.set_collapsed(0, true);
        assert!(app.pipeline_sidebar.is_collapsed(0));
        assert!(!app.pipeline_sidebar.is_collapsed(1));

        // Rebuild — without the fix this resets collapse state to default
        // (expanded) for every section.
        app.rebuild_pipeline_sidebar(None);

        assert!(
            app.pipeline_sidebar.is_collapsed(0),
            "section 0 collapse state must survive rebuild",
        );
        assert!(
            !app.pipeline_sidebar.is_collapsed(1),
            "section 1 stays expanded as it was",
        );
    }

    /// Collapse state is keyed by repo name, not section index — when the
    /// section order changes (e.g. a new repo appears at index 0), the
    /// existing repos' collapse state must follow them to their new
    /// indices.
    #[test]
    fn rebuild_pipeline_sidebar_collapse_state_follows_repo_through_reorder() {
        let mut app = make_pipeline_app();
        // Collapse "api" (index 0 in the fixture).
        app.pipeline_sidebar.set_collapsed(0, true);

        // Inject a new repo's issue at the front so its section becomes index 0.
        app.pipeline_issues.insert(0, PipelineIssue {
            number: 1,
            title: "New repo issue".to_string(),
            body: String::new(),
            repo_slug: "new/repo".to_string(),
            coord_repo: None,
            matched_labels: vec!["coord".to_string()],
            all_labels: vec!["coord".to_string()],
            is_closed: false,
        });

        app.rebuild_pipeline_sidebar(None);

        // After rebuild: pipeline_repo_names should be [new/repo, api, other/repo].
        // The new repo at index 0 stays expanded; "api" (now index 1) keeps its
        // collapsed=true; "other/repo" (now index 2) stays expanded.
        let names = &app.pipeline_repo_names;
        assert_eq!(names.iter().position(|n| n == "api"), Some(1));
        assert!(
            app.pipeline_sidebar.is_collapsed(1),
            "api's collapsed state followed it from index 0 to index 1",
        );
        assert!(
            !app.pipeline_sidebar.is_collapsed(0),
            "new section is not retroactively collapsed",
        );
    }

    /// The combined scenario from the user's report: refresh swaps
    /// pipeline_issues, selection is restored via the explicit override,
    /// AND the scroll offset is preserved so the visible area doesn't move.
    #[test]
    fn rebuild_pipeline_sidebar_preserves_both_selection_and_scroll() {
        let mut app = make_pipeline_app();
        app.pipeline_sel = Some(0);
        app.pipeline_sidebar.set_panel_scroll(17.5);

        let prev_sel = app.capture_pipeline_selection_id();
        let mut new_issues = vec![PipelineIssue {
            number: 7,
            title: "Just appeared".to_string(),
            body: String::new(),
            repo_slug: "acme/api".to_string(),
            coord_repo: Some("api".to_string()),
            matched_labels: vec!["coord".to_string()],
            all_labels: vec!["coord".to_string()],
            is_closed: false,
        }];
        new_issues.extend(app.pipeline_issues.clone());
        app.pipeline_issues = new_issues;

        app.rebuild_pipeline_sidebar(prev_sel);

        assert_eq!(
            app.pipeline_sel
                .and_then(|i| app.pipeline_issues.get(i))
                .map(|i| i.number),
            Some(42),
            "selection still on the original issue",
        );
        assert_eq!(
            app.pipeline_sidebar.panel_scroll(),
            17.5,
            "scroll preserved alongside selection",
        );
    }

    #[test]
    fn stage_status_for_pending_when_no_assignment_exists() {
        // Open issue with no assignments → Pending (waiting to be dispatched).
        let app = make_pipeline_app();
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.stage_status_for(issue, "work"), StageStatus::Pending);
        assert_eq!(app.stage_status_for(issue, "review"), StageStatus::Pending);
    }

    // ── #193: stale downstream stages ─────────────────────────────────────────

    /// Helper: an assignment of `kind` (work/review/etc.) for issue 42 on `api`
    /// at the given dispatched_at, terminal status `done` unless overridden.
    fn _stage_assignment(
        id: &str,
        kind: &str,
        dispatched_at: f64,
        status: &str,
    ) -> Assignment {
        Assignment {
            id: id.to_string(),
            repo: "api".to_string(),
            issue_number: 42,
            issue_title: "Add cool thing".to_string(),
            machine: "m1".to_string(),
            status: status.to_string(),
            branch: None,
            model: None,
            dispatched_at: Some(dispatched_at),
            finished_at: Some(dispatched_at + 60.0),
            exit_code: if status == "failed" { Some(1) } else { Some(0) },
            assignment_type: Some(kind.to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        }
    }

    #[test]
    fn stage_status_stale_when_upstream_redispatched_after_done() {
        // The user's #214 case: Work was re-dispatched after a request-changes
        // review. Previously Review showed Done (green); should now show Stale.
        let mut app = make_pipeline_app();
        app.data.assignments.push(_stage_assignment("w1", "work", 100.0, "done"));
        app.data.assignments.push(_stage_assignment("r1", "review", 200.0, "done"));
        // Work re-dispatched (later than the review).
        app.data.assignments.push(_stage_assignment("w2", "work", 300.0, "done"));
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.stage_status_for(issue, "work"), StageStatus::Done);
        assert_eq!(app.stage_status_for(issue, "review"), StageStatus::Stale);
    }

    #[test]
    fn stage_status_stale_when_upstream_redispatched_after_failed() {
        // A Failed review against an older Work is still Stale once Work re-runs:
        // the failure was about a diff that no longer exists.
        let mut app = make_pipeline_app();
        app.data.assignments.push(_stage_assignment("w1", "work", 100.0, "done"));
        app.data.assignments.push(_stage_assignment("r1", "review", 200.0, "failed"));
        app.data.assignments.push(_stage_assignment("w2", "work", 300.0, "done"));
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.stage_status_for(issue, "review"), StageStatus::Stale);
    }

    #[test]
    fn stage_status_done_when_no_upstream_redispatch() {
        // Same setup minus the re-dispatch → Review stays Done (still trustworthy).
        let mut app = make_pipeline_app();
        app.data.assignments.push(_stage_assignment("w1", "work", 100.0, "done"));
        app.data.assignments.push(_stage_assignment("r1", "review", 200.0, "done"));
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.stage_status_for(issue, "review"), StageStatus::Done);
    }

    #[test]
    fn stage_status_first_stage_never_stale() {
        // Work is the first dispatchable stage; there's no upstream to invalidate
        // it. Even with multiple dispatched_at values, latest wins as Done.
        let mut app = make_pipeline_app();
        app.data.assignments.push(_stage_assignment("w1", "work", 100.0, "done"));
        app.data.assignments.push(_stage_assignment("w2", "work", 300.0, "done"));
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.stage_status_for(issue, "work"), StageStatus::Done);
    }

    #[test]
    fn stage_status_active_beats_staleness_when_upstream_running() {
        // If a stage has a Running assignment, that wins over any verdict —
        // staleness only matters for terminal states.
        let mut app = make_pipeline_app();
        app.data.assignments.push(_stage_assignment("w1", "work", 100.0, "done"));
        app.data.assignments.push(_stage_assignment("r1", "review", 200.0, "running"));
        app.data.assignments.push(_stage_assignment("w2", "work", 300.0, "done"));
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.stage_status_for(issue, "review"), StageStatus::Active);
    }

    #[test]
    fn upstream_max_dispatched_at_returns_none_for_first_stage() {
        let mut app = make_pipeline_app();
        app.data.assignments.push(_stage_assignment("w1", "work", 100.0, "done"));
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.upstream_max_dispatched_at(issue, "work"), None);
    }

    #[test]
    fn upstream_max_dispatched_at_aggregates_across_all_prior_stages() {
        // Pipeline is work → review → merge. Querying upstream of merge should
        // see the latest dispatch across BOTH work AND review.
        let mut app = make_pipeline_app();
        app.data.assignments.push(_stage_assignment("w1", "work", 100.0, "done"));
        app.data.assignments.push(_stage_assignment("r1", "review", 250.0, "done"));
        app.data.assignments.push(_stage_assignment("w2", "work", 150.0, "done"));
        let issue = &app.pipeline_issues[0];
        // Max across {work,review} prior to merge = 250 (the review).
        assert_eq!(app.upstream_max_dispatched_at(issue, "merge"), Some(250.0));
    }

    #[test]
    fn pipeline_widget_attaches_retry_to_stale_stage_when_prior_settled() {
        // Stale + upstream Done → [Retry] button so the user can re-run.
        let mut app = make_pipeline_app();
        app.data.assignments.push(_stage_assignment("w1", "work", 100.0, "done"));
        app.data.assignments.push(_stage_assignment("r1", "review", 200.0, "done"));
        app.data.assignments.push(_stage_assignment("w2", "work", 300.0, "done"));
        app.pipeline_sel = Some(0);
        let view = app.build_pipeline_widget().expect("widget");
        // Stage layout: work, review, merge.
        let review = &view.stages[1];
        assert_eq!(review.status, StageStatus::Stale);
        assert_eq!(review.action.as_deref(), Some("Retry"));
    }

    #[test]
    fn pipeline_widget_no_retry_on_stale_when_upstream_still_active() {
        // Stale + upstream Running → no button (waiting for upstream to settle
        // before user can act).
        let mut app = make_pipeline_app();
        app.data.assignments.push(_stage_assignment("w1", "work", 100.0, "done"));
        app.data.assignments.push(_stage_assignment("r1", "review", 200.0, "done"));
        app.data.assignments.push(_stage_assignment("w2", "work", 300.0, "running"));
        app.pipeline_sel = Some(0);
        let view = app.build_pipeline_widget().expect("widget");
        let review = &view.stages[1];
        assert_eq!(review.status, StageStatus::Stale);
        assert_eq!(review.action, None);
    }

    // ── #200: Test gate ──────────────────────────────────────────────────────

    /// Build a pipeline app whose default gates include "test" (the production
    /// default). The make_pipeline_app fixture uses ["review", "merge"] which
    /// predates #200; this helper inserts the Test gate.
    fn make_pipeline_app_with_test_gate() -> CoordApp {
        let mut app = make_pipeline_app();
        app.data.pipeline_default_gates =
            vec!["test".to_string(), "review".to_string(), "merge".to_string()];
        app
    }

    fn _work_assignment(
        id: &str,
        dispatched_at: f64,
        status: &str,
        test_state: Option<&str>,
    ) -> Assignment {
        let mut a = _stage_assignment(id, "work", dispatched_at, status);
        a.test_state = test_state.map(String::from);
        a
    }

    #[test]
    fn test_stage_pending_when_work_done_no_verdict() {
        let mut app = make_pipeline_app_with_test_gate();
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", None));
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.stage_status_for(issue, "test"), StageStatus::Pending);
    }

    #[test]
    fn test_stage_done_when_passed() {
        let mut app = make_pipeline_app_with_test_gate();
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", Some("passed")));
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.stage_status_for(issue, "test"), StageStatus::Done);
    }

    #[test]
    fn test_stage_done_when_skipped() {
        let mut app = make_pipeline_app_with_test_gate();
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", Some("skipped")));
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.stage_status_for(issue, "test"), StageStatus::Done);
    }

    #[test]
    fn test_stage_failed_when_failed_verdict() {
        let mut app = make_pipeline_app_with_test_gate();
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", Some("failed")));
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.stage_status_for(issue, "test"), StageStatus::Failed);
    }

    #[test]
    fn test_stage_pending_when_work_not_done() {
        let mut app = make_pipeline_app_with_test_gate();
        app.data.assignments.push(_work_assignment("w1", 100.0, "running", None));
        let issue = &app.pipeline_issues[0];
        // Work is Active → Test inherits Pending (nothing to gate yet).
        assert_eq!(app.stage_status_for(issue, "test"), StageStatus::Pending);
    }

    #[test]
    fn test_stage_uses_latest_work_verdict() {
        // If Work is re-dispatched, the new Work's test_state controls.
        // The stale older Work's verdict must not leak through.
        let mut app = make_pipeline_app_with_test_gate();
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", Some("passed")));
        app.data.assignments.push(_work_assignment("w2", 300.0, "done", None));
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.stage_status_for(issue, "test"), StageStatus::Pending);
    }

    #[test]
    fn test_gate_actionable_only_when_pending_and_work_done() {
        let mut app = make_pipeline_app_with_test_gate();
        app.pipeline_sel = Some(0);
        // Empty board → no work yet → not actionable.
        assert!(!app.test_gate_actionable());
        // Add running work → still not actionable.
        app.data.assignments.push(_work_assignment("w1", 100.0, "running", None));
        assert!(!app.test_gate_actionable());
        // Work done, no verdict → actionable.
        app.data.assignments[0].status = "done".into();
        assert!(app.test_gate_actionable());
        // Pass it → no longer actionable.
        app.data.assignments[0].test_state = Some("passed".into());
        assert!(!app.test_gate_actionable());
    }

    #[test]
    fn test_gate_not_actionable_when_no_test_in_pipeline() {
        // make_pipeline_app (no test gate configured) → never actionable.
        let mut app = make_pipeline_app();
        app.pipeline_sel = Some(0);
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", None));
        assert!(!app.test_gate_actionable());
    }

    #[test]
    fn pipeline_selected_work_id_returns_latest() {
        let mut app = make_pipeline_app_with_test_gate();
        app.pipeline_sel = Some(0);
        assert_eq!(app.pipeline_selected_work_id(), None);
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", None));
        app.data.assignments.push(_work_assignment("w2", 300.0, "done", None));
        assert_eq!(app.pipeline_selected_work_id(), Some("w2".to_string()));
    }

    // ── #236: Test → Review / Work-bounce handoffs ─────────────────────────

    #[test]
    fn can_dispatch_review_after_test_done_false_outside_pipeline_view() {
        let mut app = make_pipeline_app_with_test_gate();
        app.pipeline_sel = Some(0);
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", Some("passed")));
        // Default view is Board — predicate must refuse.
        assert!(!app.can_dispatch_review_after_test_done());
    }

    #[test]
    fn can_dispatch_review_after_test_done_true_when_test_passed_review_pending() {
        let mut app = make_pipeline_app_with_test_gate();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", Some("passed")));
        // No review assignment yet → Review is Pending → predicate true.
        assert!(app.can_dispatch_review_after_test_done());
    }

    #[test]
    fn can_dispatch_review_after_test_done_false_when_test_pending() {
        let mut app = make_pipeline_app_with_test_gate();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        // Work done but no verdict → Test=Pending, not Done.
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", None));
        assert!(!app.can_dispatch_review_after_test_done());
    }

    #[test]
    fn can_dispatch_review_after_test_done_false_when_test_failed() {
        let mut app = make_pipeline_app_with_test_gate();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", Some("failed")));
        // Test=Failed is the bounce-back-to-Work case, not the Review one.
        assert!(!app.can_dispatch_review_after_test_done());
    }

    #[test]
    fn can_dispatch_review_after_test_done_false_when_review_already_done() {
        let mut app = make_pipeline_app_with_test_gate();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", Some("passed")));
        // Review already done → no need for R=dispatch review affordance.
        app.data.assignments.push(_stage_assignment("r1", "review", 200.0, "done"));
        assert!(!app.can_dispatch_review_after_test_done());
    }

    #[test]
    fn can_dispatch_review_after_test_done_false_when_no_coord_repo() {
        let mut app = make_pipeline_app_with_test_gate();
        app.active_view = SidebarView::Pipeline;
        // Issue #99 in the fixture has coord_repo=None — can't dispatch.
        app.pipeline_sel = Some(1);
        // Synthesise a passed Work for issue #99 by cloning the helper
        // (the helper hard-codes issue_number=42, so adjust by hand).
        let mut a = _work_assignment("w1", 100.0, "done", Some("passed"));
        a.issue_number = 99;
        a.repo = "other/repo".to_string();
        app.data.assignments.push(a);
        assert!(!app.can_dispatch_review_after_test_done());
    }

    #[test]
    fn can_bounce_work_after_test_fail_true_when_test_failed_work_done() {
        let mut app = make_pipeline_app_with_test_gate();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", Some("failed")));
        assert!(app.can_bounce_work_after_test_fail());
    }

    #[test]
    fn can_bounce_work_after_test_fail_false_when_test_passed() {
        let mut app = make_pipeline_app_with_test_gate();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", Some("passed")));
        assert!(!app.can_bounce_work_after_test_fail());
    }

    #[test]
    fn can_bounce_work_after_test_fail_false_outside_pipeline_view() {
        let mut app = make_pipeline_app_with_test_gate();
        app.pipeline_sel = Some(0);
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", Some("failed")));
        // Default view is Board — predicate refuses regardless of state.
        assert!(!app.can_bounce_work_after_test_fail());
    }

    #[test]
    fn can_bounce_work_after_test_fail_false_when_no_test_gate_configured() {
        let mut app = make_pipeline_app(); // no "test" in default_gates
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", Some("failed")));
        // No test stage in the pipeline → predicate refuses (nothing to gate on).
        assert!(!app.can_bounce_work_after_test_fail());
    }

    // ── #245: force-merge confirm prompt ────────────────────────────────

    #[test]
    fn pending_force_merge_hint_shows_repo_scope() {
        // When pending_force_merge is Some(repo), the status bar hint
        // should announce the scoped force-merge and offer y/Esc.
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        app.pending_force_merge = Some("api".to_string());

        let bar = app.status_bar();
        let hint = bar.right_segments.iter().map(|s| s.text.clone())
            .collect::<Vec<_>>().join(" ");
        assert!(
            hint.contains("Force-merge --repo api"),
            "expected scoped force-merge hint, got: {hint}",
        );
        assert!(hint.contains("y=confirm"));
        assert!(hint.contains("Esc=cancel"));
    }

    #[test]
    fn pending_force_merge_hint_shows_whole_queue_when_no_repo() {
        // Empty repo string ⇒ no --repo flag, so the prompt warns the user
        // the override hits the whole queue.
        let mut app = make_pipeline_app();
        app.pending_force_merge = Some(String::new());
        let bar = app.status_bar();
        let hint = bar.right_segments.iter().map(|s| s.text.clone())
            .collect::<Vec<_>>().join(" ");
        assert!(
            hint.contains("whole queue"),
            "expected whole-queue warning, got: {hint}",
        );
    }

    #[test]
    fn pending_force_merge_hint_overrides_other_hints() {
        // The confirm prompt must beat the normal Pipeline/Board hints so
        // the user isn't shown a stale "m=merge" affordance while we're
        // already waiting for a y/N.
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        app.pending_force_merge = Some("api".to_string());
        let bar = app.status_bar();
        let hint = bar.right_segments.iter().map(|s| s.text.clone())
            .collect::<Vec<_>>().join(" ");
        assert!(!hint.contains("p=plan"), "default hint leaked through: {hint}");
        assert!(!hint.contains("m=merge anyway"), "stale checks-failed hint: {hint}");
    }

    // ── #253: merge-blocked-on-review predicate ────────────────────────────

    #[test]
    fn merge_blocked_on_review_false_when_no_merge_entry() {
        // No entry in merge_queue → nothing to block on.
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        assert!(!app.merge_blocked_on_review_for_selected_issue());
    }

    #[test]
    fn merge_blocked_on_review_true_when_no_review_for_pending_merge() {
        // The #233 regression scenario: a pending merge entry but no approved
        // review on the board → block.
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        app.data.merge_queue.push(MergeQueueEntry {
            assignment_id: "w42".to_string(),
            issue_number: Some(42),
            state: "pending".to_string(),
            pr_number: Some(999),
            pr_url: None,
            repo_github: "acme/api".to_string(),
        });
        assert!(app.merge_blocked_on_review_for_selected_issue());
    }

    #[test]
    fn merge_blocked_on_review_false_when_approved_review_present() {
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        app.data.merge_queue.push(MergeQueueEntry {
            assignment_id: "w42".to_string(),
            issue_number: Some(42),
            state: "pending".to_string(),
            pr_number: Some(999),
            pr_url: None,
            repo_github: "acme/api".to_string(),
        });
        // An approved review on the board unblocks the merge.
        let mut review = _stage_assignment("rev-w42", "review", 200.0, "done");
        review.review_of_assignment_id = Some("w42".to_string());
        review.review_verdict = Some("approve".to_string());
        app.data.assignments.push(review);
        assert!(!app.merge_blocked_on_review_for_selected_issue());
    }

    #[test]
    fn merge_blocked_on_review_true_when_review_requested_changes() {
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        app.data.merge_queue.push(MergeQueueEntry {
            assignment_id: "w42".to_string(),
            issue_number: Some(42),
            state: "pending".to_string(),
            pr_number: Some(999),
            pr_url: None,
            repo_github: "acme/api".to_string(),
        });
        let mut review = _stage_assignment("rev-w42", "review", 200.0, "done");
        review.review_of_assignment_id = Some("w42".to_string());
        review.review_verdict = Some("request-changes".to_string());
        app.data.assignments.push(review);
        // request-changes is not an approval → still blocked.
        assert!(app.merge_blocked_on_review_for_selected_issue());
    }

    #[test]
    fn merge_blocked_on_review_false_when_merged() {
        // An already-merged entry shouldn't trigger the hint.
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        app.data.merge_queue.push(MergeQueueEntry {
            assignment_id: "w42".to_string(),
            issue_number: Some(42),
            state: "merged".to_string(),
            pr_number: Some(999),
            pr_url: None,
            repo_github: "acme/api".to_string(),
        });
        assert!(!app.merge_blocked_on_review_for_selected_issue());
    }

    // ── #272-followup: PipelineMergeState classifier + dispatcher ────────

    #[test]
    fn pipeline_merge_state_no_queue_when_no_entry() {
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        assert_eq!(
            app.pipeline_merge_state(),
            PipelineMergeState::NoQueue { issue: 42 },
        );
    }

    #[test]
    fn pipeline_merge_state_blocked_on_review_when_request_changes() {
        // Real scenario: quadraui#166 had review_verdict='request-changes'
        // but coord merge silently no-op'd because the user's m keybind
        // dispatched plain `coord merge`.  The classifier should surface
        // this so the dispatcher can toast instead.
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        app.data.merge_queue.push(MergeQueueEntry {
            assignment_id: "w42".to_string(),
            issue_number: Some(42),
            state: "pending".to_string(),
            pr_number: Some(999),
            pr_url: None,
            repo_github: "acme/api".to_string(),
        });
        let mut review = _stage_assignment("rev-w42", "review", 200.0, "done");
        review.review_of_assignment_id = Some("w42".to_string());
        review.review_verdict = Some("request-changes".to_string());
        app.data.assignments.push(review);
        match app.pipeline_merge_state() {
            PipelineMergeState::BlockedOnReview { issue, verdict } => {
                assert_eq!(issue, 42);
                assert_eq!(verdict, "request-changes");
            }
            other => panic!("expected BlockedOnReview, got {other:?}"),
        }
    }

    #[test]
    fn pipeline_merge_state_ready_when_approved_and_no_ci_failures() {
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        app.data.merge_queue.push(MergeQueueEntry {
            assignment_id: "w42".to_string(),
            issue_number: Some(42),
            state: "pending".to_string(),
            pr_number: Some(999),
            pr_url: None,
            repo_github: "acme/api".to_string(),
        });
        let mut review = _stage_assignment("rev-w42", "review", 200.0, "done");
        review.review_of_assignment_id = Some("w42".to_string());
        review.review_verdict = Some("approve".to_string());
        app.data.assignments.push(review);
        match app.pipeline_merge_state() {
            PipelineMergeState::Ready { issue, repo } => {
                assert_eq!(issue, 42);
                assert_eq!(repo, "api");
            }
            other => panic!("expected Ready, got {other:?}"),
        }
    }

    #[test]
    fn pipeline_merge_state_merged_when_entry_merged() {
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        app.data.merge_queue.push(MergeQueueEntry {
            assignment_id: "w42".to_string(),
            issue_number: Some(42),
            state: "merged".to_string(),
            pr_number: Some(999),
            pr_url: None,
            repo_github: "acme/api".to_string(),
        });
        assert_eq!(
            app.pipeline_merge_state(),
            PipelineMergeState::Merged { issue: 42 },
        );
    }

    #[test]
    fn dispatch_pipeline_merge_blocked_on_review_toasts_no_spawn() {
        // The actual bug the user reported: pressing m on a row with
        // request-changes verdict should NOT silently spawn `coord
        // merge` (which would no-op).  It should toast.
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        app.data.merge_queue.push(MergeQueueEntry {
            assignment_id: "w42".to_string(),
            issue_number: Some(42),
            state: "pending".to_string(),
            pr_number: Some(999),
            pr_url: None,
            repo_github: "acme/api".to_string(),
        });
        let mut review = _stage_assignment("rev-w42", "review", 200.0, "done");
        review.review_of_assignment_id = Some("w42".to_string());
        review.review_verdict = Some("request-changes".to_string());
        app.data.assignments.push(review);

        let toasts_before = app.toasts.len();
        let acted = app.dispatch_pipeline_merge_for_selected_issue();
        assert!(acted, "dispatcher should report having handled the action");
        assert!(
            app.toasts.len() > toasts_before,
            "must surface a toast — silent no-op was the bug we're fixing",
        );
        // The toast message should mention "review" so the user knows
        // what's wrong without reading the source.
        let last = app.toasts.last().expect("toast pushed");
        let body = last.0.body.to_string();
        assert!(
            body.to_lowercase().contains("review"),
            "toast body should mention 'review', got: {body:?}",
        );
        // Most importantly: no command was spawned.  The runner stays
        // in its initial idle state.
        assert!(
            !app.command_runner.is_running(),
            "must not spawn coord merge when blocked on review",
        );
    }

    #[test]
    fn dispatch_pipeline_merge_no_queue_toasts_no_spawn() {
        // When the user clicks Merge on an issue that hasn't been
        // enqueued yet, surface a clear toast rather than running
        // `coord merge` against an empty queue.
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);

        let toasts_before = app.toasts.len();
        let acted = app.dispatch_pipeline_merge_for_selected_issue();
        assert!(acted);
        assert!(app.toasts.len() > toasts_before);
        let body = app.toasts.last().expect("toast").0.body.to_string();
        assert!(
            body.to_lowercase().contains("no pr") || body.to_lowercase().contains("queue"),
            "toast should explain the no-queue state, got: {body:?}",
        );
        assert!(!app.command_runner.is_running());
    }

    #[test]
    fn merge_button_disabled_when_blocked_on_review() {
        // The toolbar Merge button should render disabled when the
        // selected issue can't actually be merged — the primitive's
        // hit_test then refuses clicks and the user sees the dimmed
        // affordance.
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        app.data.merge_queue.push(MergeQueueEntry {
            assignment_id: "w42".to_string(),
            issue_number: Some(42),
            state: "pending".to_string(),
            pr_number: Some(999),
            pr_url: None,
            repo_github: "acme/api".to_string(),
        });
        let mut review = _stage_assignment("rev-w42", "review", 200.0, "done");
        review.review_of_assignment_id = Some("w42".to_string());
        review.review_verdict = Some("request-changes".to_string());
        app.data.assignments.push(review);

        let bar = app.panel_toolbar().expect("Pipeline toolbar present");
        let merge_btn = bar
            .buttons
            .iter()
            .find_map(|b| match b {
                ToolbarButton::Action { id, enabled, label, .. }
                    if id.as_str() == "toolbar:merge" =>
                {
                    Some((label.clone(), *enabled))
                }
                _ => None,
            })
            .expect("Merge button present on Pipeline toolbar");
        assert!(
            merge_btn.0.contains("[M]erge"),
            "found wrong button by id: label={:?}",
            merge_btn.0,
        );
        assert!(
            !merge_btn.1,
            "Merge button must render disabled when review blocks the merge",
        );
    }

    // ── #235: Phase 1 Test-stage build ────────────────────────────────────

    /// Synthetic build job that never receives a completion message. The
    /// `_tx` is dropped at the end of the call site, which would normally
    /// turn the channel into "Disconnected", so callers MUST keep the
    /// returned `Sender` alive for the duration of the assertion.
    fn _inject_test_build_job(
        app: &mut CoordApp,
        work_id: &str,
        issue_number: u64,
        branch: &str,
    ) -> std::sync::mpsc::Sender<TestBuildOutcome> {
        let (tx, rx) = std::sync::mpsc::channel::<TestBuildOutcome>();
        app.test_build_jobs.insert(
            work_id.to_string(),
            TestBuildJob {
                work_id: work_id.to_string(),
                issue_number,
                branch: branch.to_string(),
                log_path: PathBuf::from("/tmp/test-build-fixture.log"),
                started_at: Instant::now(),
                rx,
            },
        );
        tx
    }

    #[test]
    fn can_trigger_test_build_requires_pipeline_view_and_branch() {
        let mut app = make_pipeline_app_with_test_gate();
        app.pipeline_sel = Some(0);
        // Empty board → no work → not actionable → false.
        assert!(!app.can_trigger_test_build());

        // Work done, no branch → still false (the user has nothing local
        // to check out).
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", None));
        app.active_view = SidebarView::Pipeline;
        assert!(!app.can_trigger_test_build(), "no branch → no B");

        // Add a branch → now actionable.
        app.data.assignments[0].branch = Some("issue-42-x".into());
        assert!(app.can_trigger_test_build());

        // Wrong view → false even with everything else set.
        app.active_view = SidebarView::Board;
        assert!(!app.can_trigger_test_build());
    }

    #[test]
    fn can_trigger_test_build_false_while_build_in_flight() {
        let mut app = make_pipeline_app_with_test_gate();
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", None));
        app.data.assignments[0].branch = Some("issue-42-x".into());
        assert!(app.can_trigger_test_build());

        // Inject an in-flight build for the same work id → no double-trigger.
        let _tx = _inject_test_build_job(&mut app, "w1", 42, "issue-42-x");
        assert!(!app.can_trigger_test_build());
    }

    #[test]
    fn can_trigger_test_build_false_after_verdict_recorded() {
        let mut app = make_pipeline_app_with_test_gate();
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", Some("passed")));
        app.data.assignments[0].branch = Some("issue-42-x".into());
        // Verdict recorded → Test stage is Done → gate not actionable → no B.
        assert!(!app.can_trigger_test_build());
    }

    #[test]
    fn test_stage_active_with_building_label_when_build_in_flight() {
        let mut app = make_pipeline_app_with_test_gate();
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", None));
        app.data.assignments[0].branch = Some("issue-42-x".into());

        // Before build: Test is Pending.
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.test_stage_status_for(issue), StageStatus::Pending);

        // Inject in-flight build → Test goes Active.
        let _tx = _inject_test_build_job(&mut app, "w1", 42, "issue-42-x");
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.test_stage_status_for(issue), StageStatus::Active);

        // build_pipeline_widget swaps the Test stage label to "Building".
        let view = app.build_pipeline_widget().expect("widget");
        let test_stage = view.stages.iter().find(|s| s.label == "Building")
            .expect("Test stage label should be Building while job in flight");
        assert_eq!(test_stage.status, StageStatus::Active);
    }

    #[test]
    fn test_stage_active_supersedes_prior_verdict_while_rebuilding() {
        // After a Pass verdict, the user might press B again to re-test —
        // e.g. to validate after a worker re-dispatch.  While the new build
        // is in flight, the badge should be Active+Building, not Done
        // (the old verdict is stale relative to the in-flight build).
        let mut app = make_pipeline_app_with_test_gate();
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", Some("passed")));
        app.data.assignments[0].branch = Some("issue-42-x".into());

        let issue = &app.pipeline_issues[0];
        assert_eq!(app.test_stage_status_for(issue), StageStatus::Done);

        let _tx = _inject_test_build_job(&mut app, "w1", 42, "issue-42-x");
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.test_stage_status_for(issue), StageStatus::Active);
    }

    #[test]
    fn poll_test_build_jobs_clears_completed_and_pushes_toast() {
        let mut app = make_pipeline_app_with_test_gate();
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", None));
        app.data.assignments[0].branch = Some("issue-42-x".into());

        let tx = _inject_test_build_job(&mut app, "w1", 42, "issue-42-x");
        assert_eq!(app.test_build_jobs.len(), 1);
        assert!(app.toasts.is_empty());

        // Worker thread reports success.
        tx.send(TestBuildOutcome { exit_code: 0, first_error: String::new() }).unwrap();
        // Empty channel & non-empty queue: nothing happens until we poll.
        assert!(app.poll_test_build_jobs());
        assert_eq!(app.test_build_jobs.len(), 0);
        assert_eq!(app.toasts.len(), 1);
    }

    #[test]
    fn poll_test_build_jobs_reports_failure_toast_on_nonzero_exit() {
        let mut app = make_pipeline_app_with_test_gate();
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", None));
        app.data.assignments[0].branch = Some("issue-42-x".into());

        let tx = _inject_test_build_job(&mut app, "w1", 42, "issue-42-x");
        tx.send(TestBuildOutcome {
            exit_code: 2,
            first_error: "checkout aborted: local changes".to_string(),
        })
        .unwrap();
        assert!(app.poll_test_build_jobs());
        assert_eq!(app.toasts.len(), 1);
        let (item, _, severity) = &app.toasts[0];
        assert_eq!(*severity, ToastSeverity::Error);
        // Failure toast must include the first error line so the user
        // doesn't have to cat the log to see what happened.
        assert!(
            item.body.contains("checkout aborted: local changes"),
            "toast body should include first_error; got: {}",
            item.body
        );
    }

    #[test]
    fn poll_test_build_jobs_reports_failure_when_worker_disappears() {
        // Dropping the Sender without a send turns the channel into
        // Disconnected — we must surface that as a failure toast so the
        // job doesn't sit in the map forever showing Building.
        let mut app = make_pipeline_app_with_test_gate();
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", None));
        app.data.assignments[0].branch = Some("issue-42-x".into());

        let tx = _inject_test_build_job(&mut app, "w1", 42, "issue-42-x");
        drop(tx); // simulate the worker thread panicking before sending
        assert!(app.poll_test_build_jobs());
        assert_eq!(app.test_build_jobs.len(), 0);
        assert_eq!(app.toasts.len(), 1);
        let severity = &app.toasts[0].2;
        assert_eq!(*severity, ToastSeverity::Error);
    }

    #[test]
    fn poll_test_build_jobs_noop_when_empty() {
        let mut app = make_pipeline_app_with_test_gate();
        assert!(!app.poll_test_build_jobs());
    }

    // ── Issue #212: closed-without-pipeline fixes ─────────────────────────

    #[test]
    fn issue_has_any_assignment_false_when_no_assignments() {
        let app = make_pipeline_app();
        let issue = &app.pipeline_issues[0];
        assert!(!app.issue_has_any_assignment(issue));
    }

    #[test]
    fn issue_has_any_assignment_true_when_assignment_exists() {
        let mut app = make_pipeline_app();
        app.data.assignments.push(Assignment {
            id: "a1".to_string(),
            repo: "api".to_string(),
            issue_number: 42,
            issue_title: "Add cool thing".to_string(),
            machine: "m1".to_string(),
            status: "done".to_string(),
            branch: None,
            model: None,
            dispatched_at: Some(1.0),
            finished_at: Some(2.0),
            exit_code: Some(0),
            assignment_type: Some("work".to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        });
        let issue = &app.pipeline_issues[0];
        assert!(app.issue_has_any_assignment(issue));
    }

    #[test]
    fn stage_status_for_skipped_when_closed_and_no_assignment() {
        // Closed issue with no assignments → Skipped (not Pending).
        let mut app = make_pipeline_app();
        app.pipeline_issues[0].is_closed = true;
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.stage_status_for(issue, "work"), StageStatus::Skipped);
        assert_eq!(app.stage_status_for(issue, "review"), StageStatus::Skipped);
        assert_eq!(app.stage_status_for(issue, "merge"), StageStatus::Skipped);
    }

    #[test]
    fn stage_status_for_done_overrides_skipped_when_assignment_exists() {
        // Closed issue WITH a done work assignment → work stage is Done, not Skipped.
        let mut app = make_pipeline_app();
        app.pipeline_issues[0].is_closed = true;
        app.data.assignments.push(Assignment {
            id: "w1".to_string(),
            repo: "api".to_string(),
            issue_number: 42,
            issue_title: "Add cool thing".to_string(),
            machine: "m1".to_string(),
            status: "done".to_string(),
            branch: None,
            model: None,
            dispatched_at: Some(1.0),
            finished_at: Some(2.0),
            exit_code: Some(0),
            assignment_type: Some("work".to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        });
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.stage_status_for(issue, "work"), StageStatus::Done);
        // Review has no assignment — closed → Skipped.
        assert_eq!(app.stage_status_for(issue, "review"), StageStatus::Skipped);
    }

    #[test]
    fn derive_current_stage_done_for_closed_no_pipeline() {
        // Closed with zero assignment rows → badge should say "done".
        let mut app = make_pipeline_app();
        app.pipeline_issues[0].is_closed = true;
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.derive_current_stage(issue), "done");
    }

    #[test]
    fn derive_current_stage_done_for_closed_partial_pipeline() {
        // Closed with work done but review/merge skipped → badge still "done".
        let mut app = make_pipeline_app();
        app.pipeline_issues[0].is_closed = true;
        app.data.assignments.push(Assignment {
            id: "w1".to_string(),
            repo: "api".to_string(),
            issue_number: 42,
            issue_title: "Add cool thing".to_string(),
            machine: "m1".to_string(),
            status: "done".to_string(),
            branch: None,
            model: None,
            dispatched_at: Some(1.0),
            finished_at: Some(2.0),
            exit_code: Some(0),
            assignment_type: Some("work".to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        });
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.derive_current_stage(issue), "done");
    }

    #[test]
    fn build_pipeline_widget_none_for_closed_no_pipeline() {
        // Closed issue with zero assignment rows → no pipeline widget.
        let mut app = make_pipeline_app();
        app.pipeline_issues[0].is_closed = true;
        assert!(app.build_pipeline_widget().is_none());
    }

    #[test]
    fn build_pipeline_widget_some_for_closed_with_assignments() {
        // Closed issue that DID go through coord → widget is still shown.
        let mut app = make_pipeline_app();
        app.pipeline_issues[0].is_closed = true;
        app.data.assignments.push(Assignment {
            id: "w1".to_string(),
            repo: "api".to_string(),
            issue_number: 42,
            issue_title: "Add cool thing".to_string(),
            machine: "m1".to_string(),
            status: "done".to_string(),
            branch: None,
            model: None,
            dispatched_at: Some(1.0),
            finished_at: Some(2.0),
            exit_code: Some(0),
            assignment_type: Some("work".to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        });
        let view = app.build_pipeline_widget().unwrap();
        // Work stage ran → Done.
        assert_eq!(view.stages[0].status, StageStatus::Done);
        // Review/merge not run but issue is closed → Skipped (not Pending).
        assert_eq!(view.stages[1].status, StageStatus::Skipped);
        assert_eq!(view.stages[2].status, StageStatus::Skipped);
        // No Go/Retry actions on any stage (issue is closed).
        for s in &view.stages {
            assert!(s.action.is_none(), "stage {} should have no action", s.label);
        }
    }

    #[test]
    fn pipeline_placeholder_closed_without_pipeline_message() {
        // Selecting a closed issue with no assignments shows the "closed without
        // coord pipeline" message in the placeholder list.
        let mut app = make_pipeline_app();
        app.pipeline_issues[0].is_closed = true;
        // pipeline_sel is already 0 from make_pipeline_app.
        let list = app.pipeline_placeholder_list();
        let text: String = list
            .items
            .iter()
            .flat_map(|i| i.text.spans.iter().map(|s| s.text.as_str()))
            .collect::<Vec<_>>()
            .join(" ");
        assert!(
            text.contains("Closed without coord pipeline"),
            "expected closed-without-pipeline message, got: {text:?}"
        );
    }

    #[test]
    fn pipeline_stages_list_closed_no_pipeline_shows_message() {
        // Closed issue with no assignment rows → Stages tab shows message, no stage rows.
        let mut app = make_pipeline_app();
        app.pipeline_issues[0].is_closed = true;
        let list = app.pipeline_stages_list();
        let text: String = list
            .items
            .iter()
            .flat_map(|i| i.text.spans.iter().map(|s| s.text.as_str()))
            .collect::<Vec<_>>()
            .join(" ");
        assert!(
            text.contains("Closed without coord pipeline"),
            "expected closed-without-pipeline message, got: {text:?}"
        );
        // No stage headers (Work / Review / Merge) should appear.
        assert!(!text.contains("Work"), "Work stage row should be suppressed");
    }

    #[test]
    fn pipeline_stages_list_closed_with_assignments_shows_stages() {
        // Closed issue that went through coord → Stages tab still shows stages.
        let mut app = make_pipeline_app();
        app.pipeline_issues[0].is_closed = true;
        app.data.assignments.push(Assignment {
            id: "w1".to_string(),
            repo: "api".to_string(),
            issue_number: 42,
            issue_title: "Add cool thing".to_string(),
            machine: "m1".to_string(),
            status: "done".to_string(),
            branch: None,
            model: None,
            dispatched_at: Some(1.0),
            finished_at: Some(2.0),
            exit_code: Some(0),
            assignment_type: Some("work".to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        });
        let list = app.pipeline_stages_list();
        let text: String = list
            .items
            .iter()
            .flat_map(|i| i.text.spans.iter().map(|s| s.text.as_str()))
            .collect::<Vec<_>>()
            .join(" ");
        // Stage headers must appear.
        assert!(text.contains("Work"), "Work header missing");
        assert!(text.contains("Review"), "Review header missing");
        // No "closed without coord pipeline" message when there are assignments.
        assert!(
            !text.contains("Closed without coord pipeline"),
            "should not show closed-no-pipeline message when assignments exist"
        );
    }

    #[test]
    fn stage_status_for_running_work_marks_active() {
        let mut app = make_pipeline_app();
        app.data.assignments.push(Assignment {
            id: "abc".to_string(),
            repo: "api".to_string(),
            issue_number: 42,
            issue_title: "Add cool thing".to_string(),
            machine: "m1".to_string(),
            status: "running".to_string(),
            branch: None,
            model: None,
            dispatched_at: Some(1.0),
            finished_at: None,
            exit_code: None,
            assignment_type: Some("work".to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        });
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.stage_status_for(issue, "work"), StageStatus::Active);
    }

    #[test]
    fn stage_status_for_done_work_marks_done() {
        let mut app = make_pipeline_app();
        app.data.assignments.push(Assignment {
            id: "abc".to_string(),
            repo: "api".to_string(),
            issue_number: 42,
            issue_title: "Add cool thing".to_string(),
            machine: "m1".to_string(),
            status: "done".to_string(),
            branch: None,
            model: None,
            dispatched_at: Some(1.0),
            finished_at: Some(2.0),
            exit_code: Some(0),
            assignment_type: Some("work".to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        });
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.stage_status_for(issue, "work"), StageStatus::Done);
    }

    /// When a Work stage fails and is then retried successfully, the stage
    /// must reflect the LATEST attempt (Done), not the older Failed one. The
    /// board-level sort puts failed before done, so stage_status_for can't
    /// just take the first match — it must pick the latest by dispatched_at.
    #[test]
    fn stage_status_for_failed_then_retried_done_is_done() {
        let mut app = make_pipeline_app();
        // Older failed attempt.
        app.data.assignments.push(Assignment {
            id: "old-failed".to_string(),
            repo: "api".to_string(),
            issue_number: 42,
            issue_title: "Add cool thing".to_string(),
            machine: "m1".to_string(),
            status: "failed".to_string(),
            branch: None,
            model: None,
            dispatched_at: Some(1.0),
            finished_at: Some(1.5),
            exit_code: Some(1),
            assignment_type: Some("work".to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        });
        // Newer successful retry.
        app.data.assignments.push(Assignment {
            id: "new-done".to_string(),
            repo: "api".to_string(),
            issue_number: 42,
            issue_title: "Add cool thing".to_string(),
            machine: "m2".to_string(),
            status: "done".to_string(),
            branch: None,
            model: None,
            dispatched_at: Some(10.0),
            finished_at: Some(11.0),
            exit_code: Some(0),
            assignment_type: Some("work".to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        });
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.stage_status_for(issue, "work"), StageStatus::Done);
    }

    #[test]
    fn stage_status_for_assignment_in_other_repo_is_ignored() {
        // An assignment for the same issue number but a different coord repo
        // must not pollute the stage status of a different issue.
        let mut app = make_pipeline_app();
        app.data.assignments.push(Assignment {
            id: "abc".to_string(),
            repo: "different-repo".to_string(),
            issue_number: 42,
            issue_title: "unrelated".to_string(),
            machine: "m1".to_string(),
            status: "done".to_string(),
            branch: None,
            model: None,
            dispatched_at: Some(1.0),
            finished_at: Some(2.0),
            exit_code: Some(0),
            assignment_type: Some("work".to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        });
        let issue = &app.pipeline_issues[0];
        // issue.coord_repo == "api", assignment.repo == "different-repo" →
        // assignment is filtered out → status stays Pending.
        assert_eq!(app.stage_status_for(issue, "work"), StageStatus::Pending);
    }

    #[test]
    fn build_pipeline_widget_attaches_go_to_first_pending_work() {
        let app = make_pipeline_app();
        let view = app.build_pipeline_widget().unwrap();
        assert_eq!(view.stages.len(), 3);
        assert_eq!(view.stages[0].label, "Work");
        // Only the work stage gets a Go button (and only when Pending).
        assert_eq!(view.stages[0].action.as_deref(), Some("Go"));
        assert!(view.stages[1].action.is_none());
        assert!(view.stages[2].action.is_none());
    }

    #[test]
    fn build_pipeline_widget_no_go_for_unmappable_repo() {
        let mut app = make_pipeline_app();
        // Select the second issue (the one with no coord_repo mapping).
        app.pipeline_sel = Some(1);
        let view = app.build_pipeline_widget().unwrap();
        // Without a coord_repo, Go is suppressed — we can't dispatch.
        for stage in &view.stages {
            assert!(stage.action.is_none(), "stage {:?} should have no action", stage.label);
        }
    }

    /// Work done, no review assignment → [Go] moves to the Review stage.
    #[test]
    fn build_pipeline_widget_go_on_review_when_work_done() {
        let mut app = make_pipeline_app();
        app.data.assignments.push(Assignment {
            id: "w1".to_string(),
            repo: "api".to_string(),
            issue_number: 42,
            issue_title: "Add cool thing".to_string(),
            machine: "m1".to_string(),
            status: "done".to_string(),
            branch: None,
            model: None,
            dispatched_at: Some(1.0),
            finished_at: Some(2.0),
            exit_code: Some(0),
            assignment_type: Some("work".to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        });
        let view = app.build_pipeline_widget().unwrap();
        assert_eq!(view.stages[0].label, "Work");
        assert_eq!(view.stages[0].status, StageStatus::Done);
        assert!(view.stages[0].action.is_none(), "Work is done — no Go");
        assert_eq!(
            view.stages[1].action.as_deref(),
            Some("Go"),
            "Review should now own the Go button"
        );
        assert!(view.stages[2].action.is_none(), "Merge still gated on review");
    }

    /// Work + review both done, merge_queue empty → [Go] on Merge stage.
    #[test]
    fn build_pipeline_widget_go_on_merge_when_review_done() {
        let mut app = make_pipeline_app();
        app.data.assignments.push(Assignment {
            id: "w1".to_string(),
            repo: "api".to_string(),
            issue_number: 42,
            issue_title: "Add cool thing".to_string(),
            machine: "m1".to_string(),
            status: "done".to_string(),
            branch: None,
            model: None,
            dispatched_at: Some(1.0),
            finished_at: Some(2.0),
            exit_code: Some(0),
            assignment_type: Some("work".to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        });
        app.data.assignments.push(Assignment {
            id: "r1".to_string(),
            repo: "api".to_string(),
            issue_number: 42,
            issue_title: "Add cool thing".to_string(),
            machine: "m1".to_string(),
            status: "done".to_string(),
            branch: None,
            model: None,
            dispatched_at: Some(3.0),
            finished_at: Some(4.0),
            exit_code: Some(0),
            assignment_type: Some("review".to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        });
        let view = app.build_pipeline_widget().unwrap();
        assert_eq!(view.stages[0].status, StageStatus::Done);
        assert_eq!(view.stages[1].status, StageStatus::Done);
        assert!(view.stages[0].action.is_none());
        assert!(view.stages[1].action.is_none());
        assert_eq!(view.stages[2].action.as_deref(), Some("Go"));
    }

    /// A `merged` row in merge_queue makes the Merge stage Done.
    #[test]
    fn merge_stage_status_done_from_merge_queue() {
        let mut app = make_pipeline_app();
        app.data.merge_queue.push(MergeQueueEntry {
            assignment_id: "w1".to_string(),
            issue_number: Some(42),
            state: "merged".to_string(),
            pr_number: Some(7),
            pr_url: Some("https://example/pr/7".to_string()),
            repo_github: "acme/api".to_string(),
        });
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.stage_status_for(issue, "merge"), StageStatus::Done);
    }

    /// An `open` row in merge_queue marks the Merge stage Active.
    #[test]
    fn merge_stage_status_active_from_open_pr() {
        let mut app = make_pipeline_app();
        app.data.merge_queue.push(MergeQueueEntry {
            assignment_id: "w1".to_string(),
            issue_number: Some(42),
            state: "open".to_string(),
            pr_number: Some(7),
            pr_url: None,
            repo_github: "acme/api".to_string(),
        });
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.stage_status_for(issue, "merge"), StageStatus::Active);
    }

    /// Merge stage already merged → no [Go] anywhere; full pipeline Done.
    #[test]
    fn build_pipeline_widget_no_go_when_all_done() {
        let mut app = make_pipeline_app();
        for stage_name in ["work", "review"] {
            app.data.assignments.push(Assignment {
                id: stage_name.to_string(),
                repo: "api".to_string(),
                issue_number: 42,
                issue_title: "Add cool thing".to_string(),
                machine: "m1".to_string(),
                status: "done".to_string(),
                branch: None,
                model: None,
                dispatched_at: Some(1.0),
                finished_at: Some(2.0),
                exit_code: Some(0),
                assignment_type: Some(stage_name.to_string()),
                test_state: None,
                review_verdict: None,
                review_of_assignment_id: None,
                cost_usd: None,
                smoke_tests: None,
                review_findings: None,
            });
        }
        app.data.merge_queue.push(MergeQueueEntry {
            assignment_id: "w1".to_string(),
            issue_number: Some(42),
            state: "merged".to_string(),
            pr_number: Some(7),
            pr_url: None,
            repo_github: "acme/api".to_string(),
        });
        let view = app.build_pipeline_widget().unwrap();
        for stage in &view.stages {
            assert_eq!(stage.status, StageStatus::Done);
            assert!(stage.action.is_none());
        }
    }

    /// Failed Work stage gets a [Retry] action; later Pending stages stay quiet.
    #[test]
    fn build_pipeline_widget_retry_on_failed_work() {
        let mut app = make_pipeline_app();
        app.data.assignments.push(Assignment {
            id: "w-failed".to_string(),
            repo: "api".to_string(),
            issue_number: 42,
            issue_title: "Add cool thing".to_string(),
            machine: "m1".to_string(),
            status: "failed".to_string(),
            branch: None,
            model: None,
            dispatched_at: Some(1.0),
            finished_at: Some(2.0),
            exit_code: Some(1),
            assignment_type: Some("work".to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        });
        let view = app.build_pipeline_widget().unwrap();
        assert_eq!(view.stages[0].status, StageStatus::Failed);
        assert_eq!(view.stages[0].action.as_deref(), Some("Retry"));
        // Review still Pending but its predecessor (Work) is Failed, not
        // Done — no Go on Review.
        assert!(view.stages[1].action.is_none());
        assert!(view.stages[2].action.is_none());
    }

    /// Failed merge_queue entry → [Retry] on Merge.
    #[test]
    fn build_pipeline_widget_retry_on_failed_merge() {
        let mut app = make_pipeline_app();
        // Work + Review done so the merge stage is reachable on the timeline.
        for stage_name in ["work", "review"] {
            app.data.assignments.push(Assignment {
                id: stage_name.to_string(),
                repo: "api".to_string(),
                issue_number: 42,
                issue_title: "Add cool thing".to_string(),
                machine: "m1".to_string(),
                status: "done".to_string(),
                branch: None,
                model: None,
                dispatched_at: Some(1.0),
                finished_at: Some(2.0),
                exit_code: Some(0),
                assignment_type: Some(stage_name.to_string()),
                test_state: None,
                review_verdict: None,
                review_of_assignment_id: None,
                cost_usd: None,
                smoke_tests: None,
                review_findings: None,
            });
        }
        app.data.merge_queue.push(MergeQueueEntry {
            assignment_id: "w".to_string(),
            issue_number: Some(42),
            state: "failed".to_string(),
            pr_number: None,
            pr_url: None,
            repo_github: "acme/api".to_string(),
        });
        let view = app.build_pipeline_widget().unwrap();
        assert_eq!(view.stages[2].status, StageStatus::Failed);
        assert_eq!(view.stages[2].action.as_deref(), Some("Retry"));
    }

    /// Failed stage without a coord_repo mapping must not show Retry —
    /// we'd have nothing to dispatch.
    #[test]
    fn build_pipeline_widget_no_retry_without_coord_repo() {
        let mut app = make_pipeline_app();
        app.pipeline_sel = Some(1); // the unmapped issue
        app.data.assignments.push(Assignment {
            id: "x".to_string(),
            repo: "other".to_string(),
            issue_number: 99,
            issue_title: "Mystery".to_string(),
            machine: "m1".to_string(),
            status: "failed".to_string(),
            branch: None,
            model: None,
            dispatched_at: Some(1.0),
            finished_at: Some(2.0),
            exit_code: Some(1),
            assignment_type: Some("work".to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        });
        let view = app.build_pipeline_widget().unwrap();
        for stage in &view.stages {
            assert!(stage.action.is_none());
        }
    }

    /// Plan stage is prepended when pipeline_require_plan is true.
    #[test]
    fn pipeline_stage_names_prepends_plan_when_required() {
        let mut app = make_pipeline_app();
        app.data.pipeline_require_plan = true;
        assert_eq!(
            app.pipeline_stage_names(),
            vec![
                "plan".to_string(),
                "work".to_string(),
                "review".to_string(),
                "merge".to_string(),
            ]
        );
    }

    /// Plan stage is omitted when pipeline_require_plan is false (default).
    #[test]
    fn pipeline_stage_names_omits_plan_when_not_required() {
        let app = make_pipeline_app();
        // Default is false — already covered by pipeline_stage_names_prepends_work,
        // but re-assert explicitly for documentation.
        assert!(!app.data.pipeline_require_plan);
        assert_eq!(
            app.pipeline_stage_names(),
            vec!["work".to_string(), "review".to_string(), "merge".to_string()]
        );
    }

    /// With pipeline_require_plan on, Plan is Pending → [Go] on Plan, no Go on Work.
    #[test]
    fn build_pipeline_widget_go_on_plan_when_required() {
        let mut app = make_pipeline_app();
        app.data.pipeline_require_plan = true;
        let view = app.build_pipeline_widget().unwrap();
        assert_eq!(view.stages[0].label, "Plan");
        assert_eq!(view.stages[0].action.as_deref(), Some("Go"));
        // Work and later stages are gated behind a Done Plan.
        for stage in &view.stages[1..] {
            assert!(stage.action.is_none());
        }
    }

    /// With pipeline_require_plan on, a Done plan assignment must NOT make
    /// the Work stage Done — they're now distinct stage types.
    #[test]
    fn stage_status_for_plan_done_does_not_advance_work_when_plan_gate_on() {
        let mut app = make_pipeline_app();
        app.data.pipeline_require_plan = true;
        app.data.assignments.push(Assignment {
            id: "p1".to_string(),
            repo: "api".to_string(),
            issue_number: 42,
            issue_title: "Add cool thing".to_string(),
            machine: "m1".to_string(),
            status: "done".to_string(),
            branch: None,
            model: None,
            dispatched_at: Some(1.0),
            finished_at: Some(2.0),
            exit_code: Some(0),
            assignment_type: Some("plan".to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        });
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.stage_status_for(issue, "plan"), StageStatus::Done);
        assert_eq!(app.stage_status_for(issue, "work"), StageStatus::Pending);
        // Now Work stage owns the [Go] button.
        let view = app.build_pipeline_widget().unwrap();
        assert_eq!(view.stages[0].status, StageStatus::Done);
        assert!(view.stages[0].action.is_none());
        assert_eq!(view.stages[1].label, "Work");
        assert_eq!(view.stages[1].action.as_deref(), Some("Go"));
    }

    /// find_done_plan_assignment_id returns the plan assignment when present.
    #[test]
    fn find_done_plan_assignment_id_returns_done_plan() {
        let mut app = make_pipeline_app();
        app.data.assignments.push(Assignment {
            id: "p-done".to_string(),
            repo: "api".to_string(),
            issue_number: 42,
            issue_title: "Add cool thing".to_string(),
            machine: "m1".to_string(),
            status: "done".to_string(),
            branch: None,
            model: None,
            dispatched_at: Some(1.0),
            finished_at: Some(2.0),
            exit_code: Some(0),
            assignment_type: Some("plan".to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        });
        let issue = &app.pipeline_issues[0].clone();
        let id = app.find_done_plan_assignment_id(issue, "api");
        assert_eq!(id, Some("p-done".to_string()));
    }

    /// find_done_plan_assignment_id returns None for running or absent plans.
    #[test]
    fn find_done_plan_assignment_id_skips_non_done() {
        let mut app = make_pipeline_app();
        app.data.assignments.push(Assignment {
            id: "p-running".to_string(),
            repo: "api".to_string(),
            issue_number: 42,
            issue_title: "Add cool thing".to_string(),
            machine: "m1".to_string(),
            status: "running".to_string(),
            branch: None,
            model: None,
            dispatched_at: Some(1.0),
            finished_at: None,
            exit_code: None,
            assignment_type: Some("plan".to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        });
        let issue = &app.pipeline_issues[0].clone();
        assert_eq!(app.find_done_plan_assignment_id(issue, "api"), None);
    }

    /// Stages tab list includes one row per stage and detail rows for
    /// assignments that exist.
    #[test]
    fn pipeline_stages_list_renders_stage_headers_and_details() {
        let mut app = make_pipeline_app();
        app.data.assignments.push(Assignment {
            id: "abcdef1234567890".to_string(),
            repo: "api".to_string(),
            issue_number: 42,
            issue_title: "Add cool thing".to_string(),
            machine: "m1".to_string(),
            status: "done".to_string(),
            branch: Some("issue-42-cool".to_string()),
            model: Some("sonnet".to_string()),
            dispatched_at: Some(1.0),
            finished_at: Some(2.0),
            exit_code: Some(0),
            assignment_type: Some("work".to_string()),
            test_state: None,
            review_verdict: None,
            review_of_assignment_id: None,
            cost_usd: None,
            smoke_tests: None,
            review_findings: None,
        });
        let list = app.pipeline_stages_list();
        let text_blob: String = list
            .items
            .iter()
            .flat_map(|it| it.text.spans.iter().map(|s| s.text.as_str()))
            .collect::<Vec<&str>>()
            .join("|");
        // Headers for every stage are present.
        assert!(text_blob.contains("Work"), "Work header missing");
        assert!(text_blob.contains("Review"), "Review header missing");
        assert!(text_blob.contains("Merge"), "Merge header missing");
        // Assignment short id appears.
        assert!(text_blob.contains("abcdef12"), "short id missing");
        // Branch + model render.
        assert!(text_blob.contains("issue-42-cool"), "branch missing");
        assert!(text_blob.contains("sonnet"), "model missing");
        // Empty-detail rows for stages with no assignments.
        assert!(text_blob.contains("(not started)") || text_blob.contains("(not queued)"));
    }

    /// capitalize() upper-cases the first ASCII character only.
    #[test]
    fn capitalize_first_char() {
        assert_eq!(capitalize(""), "");
        assert_eq!(capitalize("plan"), "Plan");
        assert_eq!(capitalize("work"), "Work");
        assert_eq!(capitalize("Plan"), "Plan");
    }

    /// is_dispatchable_stage covers the four stages the panel can fire.
    #[test]
    fn is_dispatchable_stage_recognises_known_stages() {
        assert!(is_dispatchable_stage("plan"));
        assert!(is_dispatchable_stage("work"));
        assert!(is_dispatchable_stage("review"));
        assert!(is_dispatchable_stage("merge"));
        assert!(!is_dispatchable_stage("smoke"));
        assert!(!is_dispatchable_stage(""));
    }

    #[test]
    fn best_machine_for_picks_least_loaded_with_repo() {
        let app = CoordApp {
            data: BoardData {
                machines: vec![
                    Machine {
                        name: "busy".to_string(),
                        host: String::new(),
                        reachable: true,
                        active_count: 3,
                        repos: vec!["api".to_string()],
                    },
                    Machine {
                        name: "idle".to_string(),
                        host: String::new(),
                        reachable: true,
                        active_count: 0,
                        repos: vec!["api".to_string()],
                    },
                    Machine {
                        name: "wrong-repo".to_string(),
                        host: String::new(),
                        reachable: true,
                        active_count: 0,
                        repos: vec!["other".to_string()],
                    },
                ],
                ..BoardData::default()
            },
            ..make_test_app(BoardData::default())
        };
        let picked = app.best_machine_for("api").unwrap();
        assert_eq!(picked.name, "idle");
    }

    #[test]
    fn best_machine_for_skips_unreachable() {
        let app = CoordApp {
            data: BoardData {
                machines: vec![Machine {
                    name: "off".to_string(),
                    host: String::new(),
                    reachable: false,
                    active_count: 0,
                    repos: vec!["api".to_string()],
                }],
                ..BoardData::default()
            },
            ..make_test_app(BoardData::default())
        };
        assert!(app.best_machine_for("api").is_none());
    }

    #[test]
    fn sidebar_view_pipeline_label() {
        assert_eq!(SidebarView::Pipeline.label(), "Pipeline");
    }

    #[test]
    fn stage_badge_known_stages() {
        assert_eq!(stage_badge("work").0, "work");
        assert_eq!(stage_badge("review").0, "review");
        assert_eq!(stage_badge("merge").0, "merge");
        assert_eq!(stage_badge("done").0, "done");
    }

    #[test]
    fn pipeline_loader_defaults_when_no_meta() {
        // Sanity: load_pipeline_meta returns documented defaults when the
        // DB is missing the keys (or the table is empty).
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE board_meta (key TEXT PRIMARY KEY, value TEXT);",
        )
        .unwrap();
        let (gates, labels, repos, require_plan) = load_pipeline_meta(&conn);
        assert_eq!(gates, vec!["review".to_string(), "merge".to_string()]);
        assert_eq!(labels, vec!["coord".to_string()]);
        assert!(repos.is_empty());
        assert!(!require_plan);
    }

    #[test]
    fn pipeline_loader_reads_persisted_values() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE board_meta (key TEXT PRIMARY KEY, value TEXT);
             INSERT INTO board_meta VALUES \
              ('pipeline_default_gates', '[\"plan\",\"work\",\"smoke\"]'), \
              ('pipeline_tracked_labels', '[\"hotfix\",\"feature\"]'), \
              ('pipeline_repos', '{\"api\":\"acme/api\"}'), \
              ('pipeline_require_plan', '1');",
        )
        .unwrap();
        let (gates, labels, repos, require_plan) = load_pipeline_meta(&conn);
        assert_eq!(gates, vec!["plan", "work", "smoke"]);
        assert_eq!(labels, vec!["hotfix", "feature"]);
        assert_eq!(repos, vec![("api".to_string(), "acme/api".to_string())]);
        assert!(require_plan);
    }

    // ── Purge helpers ─────────────────────────────────────────────────────────

    /// Build an in-memory DB with the columns the purge predicates read.
    /// Only the columns referenced by the SQL need to exist; this keeps the
    /// fixture small and decoupled from the production schema migrations.
    fn make_purge_db() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE assignments (
                assignment_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                finished_at REAL
             );
             CREATE TABLE issues (
                number INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                synced_at REAL
             );",
        )
        .unwrap();
        conn
    }

    fn insert_assignment(conn: &Connection, id: &str, status: &str, finished_at: Option<f64>) {
        conn.execute(
            "INSERT INTO assignments (assignment_id, status, finished_at) VALUES (?1, ?2, ?3)",
            rusqlite::params![id, status, finished_at],
        )
        .unwrap();
    }

    fn insert_issue(conn: &Connection, number: i64, state: &str, synced_at: Option<f64>) {
        conn.execute(
            "INSERT INTO issues (number, state, synced_at) VALUES (?1, ?2, ?3)",
            rusqlite::params![number, state, synced_at],
        )
        .unwrap();
    }

    // ── count_purgeable_conn ──────────────────────────────────────────────────

    #[test]
    fn count_purgeable_counts_done_rows_older_than_cutoff() {
        let conn = make_purge_db();
        insert_assignment(&conn, "old", "done", Some(100.0));   // older than cutoff
        insert_assignment(&conn, "new", "done", Some(500.0));   // newer than cutoff
        let (a, i) = count_purgeable_conn(&conn, 300.0).unwrap();
        assert_eq!(a, 1);
        assert_eq!(i, 0);
    }

    #[test]
    fn count_purgeable_excludes_running_and_pending() {
        let conn = make_purge_db();
        insert_assignment(&conn, "r", "running", Some(0.0));
        insert_assignment(&conn, "p", "pending", Some(0.0));
        let (a, _) = count_purgeable_conn(&conn, 300.0).unwrap();
        assert_eq!(a, 0);
    }

    #[test]
    fn count_purgeable_includes_old_failed_rows() {
        let conn = make_purge_db();
        insert_assignment(&conn, "f", "failed", Some(100.0));
        let (a, _) = count_purgeable_conn(&conn, 300.0).unwrap();
        assert_eq!(a, 1);
    }

    #[test]
    fn count_purgeable_excludes_rows_with_no_finished_at() {
        let conn = make_purge_db();
        insert_assignment(&conn, "d", "done", None);
        let (a, _) = count_purgeable_conn(&conn, 300.0).unwrap();
        assert_eq!(a, 0);
    }

    #[test]
    fn count_purgeable_counts_closed_issues_older_than_cutoff() {
        let conn = make_purge_db();
        insert_issue(&conn, 1, "closed", Some(100.0));   // purgeable
        insert_issue(&conn, 2, "closed", Some(500.0));   // too fresh
        insert_issue(&conn, 3, "open", Some(50.0));      // open: never purged
        insert_issue(&conn, 4, "closed", None);          // no synced_at: never purged
        let (_, i) = count_purgeable_conn(&conn, 300.0).unwrap();
        assert_eq!(i, 1);
    }

    // ── count vs delete symmetry ─────────────────────────────────────────────

    #[test]
    fn count_matches_delete_for_mixed_data() {
        // The reviewer's bug #2: prompt showed one number, toast showed another
        // because count_purgeable saw assignments-only and the SQL also deleted
        // issues. This test guards against future drift between the two helpers.
        let conn = make_purge_db();
        insert_assignment(&conn, "a1", "done", Some(100.0));
        insert_assignment(&conn, "a2", "failed", Some(100.0));
        insert_assignment(&conn, "a3", "done", Some(500.0));  // too fresh
        insert_issue(&conn, 1, "closed", Some(100.0));
        insert_issue(&conn, 2, "closed", Some(200.0));
        insert_issue(&conn, 3, "open", Some(100.0));

        let counted = count_purgeable_conn(&conn, 300.0).unwrap();
        let deleted = purge_done_assignments_conn(&conn, 300.0).unwrap();
        assert_eq!(counted, deleted, "count and delete must agree exactly");
        assert_eq!(counted, (2, 2));
    }

    // ── board_selection_in_completed_group ────────────────────────────────────

    /// #265 helper: build an app where issue #10 is closed (on GitHub)
    /// and has a done assignment, so it lands in the Completed group.
    fn make_app_with_one_completed_issue() -> CoordApp {
        let mut app = make_app_with_assignments(vec![make_assignment_typed(
            "done", 10, "repo-a", Some("work"),
        )]);
        app.data.open_issues.push(OpenIssue {
            repo_name: "repo-a".to_string(),
            number: 10,
            title: "closed one".to_string(),
            body: String::new(),
            state: "closed".to_string(),
            labels: Vec::new(),
        });
        app.rebuild_board_sidebar();
        app
    }

    #[test]
    fn purge_guard_true_when_completed_group_header_selected() {
        // #265: a "done" assignment on a *closed* GitHub issue lands
        // in the Completed group at group_idx 0.
        let mut app = make_app_with_one_completed_issue();
        // Section 1 = repo-a (section 0 is the search form).
        // Path [0] selects the Completed group header.
        app.board_sidebar.set_active_section(Some(1));
        app.board_sidebar.set_selected_path(1, Some(vec![0]));
        assert!(app.board_selection_in_completed_group());
    }

    #[test]
    fn purge_guard_true_when_issue_row_in_completed_group_selected() {
        // Issue row within Completed group (path [group_idx, issue_idx]).
        let mut app = make_app_with_one_completed_issue();
        app.board_sidebar.set_active_section(Some(1));
        app.board_sidebar.set_selected_path(1, Some(vec![0, 0]));
        assert!(app.board_selection_in_completed_group());
    }

    #[test]
    fn purge_guard_false_when_running_group_selected() {
        // Running group only — group_idx 0 is "Running", not "Completed".
        let assignments = vec![make_assignment_typed("running", 10, "repo-a", Some("work"))];
        let mut app = make_app_with_assignments(assignments);
        app.board_sidebar.set_active_section(Some(1));
        app.board_sidebar.set_selected_path(1, Some(vec![0]));
        assert!(!app.board_selection_in_completed_group());
    }

    #[test]
    fn purge_guard_false_when_failed_group_selected() {
        let assignments = vec![make_assignment_typed("failed", 10, "repo-a", Some("work"))];
        let mut app = make_app_with_assignments(assignments);
        app.board_sidebar.set_active_section(Some(1));
        app.board_sidebar.set_selected_path(1, Some(vec![0]));
        assert!(!app.board_selection_in_completed_group());
    }

    #[test]
    fn purge_guard_false_when_no_section_active() {
        let app = make_app_default();
        assert!(!app.board_selection_in_completed_group());
    }

    // ── SSE watch overlay ────────────────────────────────────────────────────

    /// Build a `WatchSseState` with a test sender and receiver pair.
    fn make_sse_state_pair() -> (WatchSseState, std::sync::mpsc::Sender<SseWatchMsg>) {
        let (tx, rx) = std::sync::mpsc::channel::<SseWatchMsg>();
        let state = WatchSseState {
            rx,
            lines: Vec::new(),
            last_event_id: 0,
            fail_count: 0,
            first_fail_at: None,
            done: false,
            host: "localhost".to_string(),
            assignment_id: "test-id".to_string(),
            pending_tail: String::new(),
        };
        (state, tx)
    }

    #[test]
    fn drain_sse_watch_accumulates_lines() {
        let mut app = make_app_default();
        let (state, tx) = make_sse_state_pair();
        app.watch_sse = Some(state);

        // Each chunk's text has a trailing `\n` — that's what the agent
        // emits when the source bytes ended at a line boundary.
        tx.send(SseWatchMsg::Lines { last_id: 100, text: "line one\nline two\n".to_string() }).unwrap();
        tx.send(SseWatchMsg::Lines { last_id: 200, text: "line three\n".to_string() }).unwrap();

        let changed = app.drain_sse_watch();
        assert!(changed, "new lines should trigger redraw");

        let sse = app.watch_sse.as_ref().unwrap();
        assert_eq!(sse.last_event_id, 200);
        assert_eq!(sse.lines, vec!["line one", "line two", "line three"]);
        assert!(!sse.done);
    }

    #[test]
    fn drain_sse_watch_reassembles_partial_line_split_across_chunks() {
        // Regression: the agent reads the log in 4 KB chunks, so a long JSON
        // line (e.g. `{"type":"result", ..., "total_cost_usd":...}`) arrives
        // in pieces. Without reassembly the broken halves reach the parser
        // and we lose fields after the split point — surfaced as $0.00 /
        // stop=? in the watch overlay.
        let mut app = make_app_default();
        let (state, tx) = make_sse_state_pair();
        app.watch_sse = Some(state);

        // Chunk 1 has the first half of a JSON line (NO trailing newline).
        tx.send(SseWatchMsg::Lines {
            last_id: 100,
            text: "{\"type\":\"result\",\"num_turns\":37".to_string(),
        }).unwrap();
        // Chunk 2 has the rest plus the terminating newline.
        tx.send(SseWatchMsg::Lines {
            last_id: 200,
            text: ",\"total_cost_usd\":1.75}\n".to_string(),
        }).unwrap();

        app.drain_sse_watch();

        let sse = app.watch_sse.as_ref().unwrap();
        assert_eq!(sse.last_event_id, 200);
        assert_eq!(
            sse.lines,
            vec!["{\"type\":\"result\",\"num_turns\":37,\"total_cost_usd\":1.75}"],
            "the two chunks must have been joined into one line, not two"
        );
        assert!(sse.pending_tail.is_empty(), "tail should be flushed after the second chunk");
    }

    #[test]
    fn drain_sse_watch_marks_done_on_end_event() {
        let mut app = make_app_default();
        let (state, tx) = make_sse_state_pair();
        app.watch_sse = Some(state);

        // Trailing `\n` so the chunk is complete (no partial line to flush).
        tx.send(SseWatchMsg::Lines { last_id: 50, text: "some output\n".to_string() }).unwrap();
        tx.send(SseWatchMsg::Done { last_id: 99 }).unwrap();

        let changed = app.drain_sse_watch();
        assert!(changed);

        let sse = app.watch_sse.as_ref().unwrap();
        assert!(sse.done, "done should be set after End event");
        assert_eq!(sse.last_event_id, 99);
        assert_eq!(sse.lines, vec!["some output"]);
    }

    #[test]
    fn drain_sse_watch_flushes_pending_tail_on_done() {
        // If the stream ends without the writer's final newline, the partial
        // line should still surface (don't silently drop the worker's last
        // result line).
        let mut app = make_app_default();
        let (state, tx) = make_sse_state_pair();
        app.watch_sse = Some(state);

        tx.send(SseWatchMsg::Lines { last_id: 10, text: "trailing partial".to_string() }).unwrap();
        tx.send(SseWatchMsg::Done { last_id: 11 }).unwrap();

        app.drain_sse_watch();
        let sse = app.watch_sse.as_ref().unwrap();
        assert!(sse.done);
        assert_eq!(sse.lines, vec!["trailing partial"]);
    }

    #[test]
    fn drain_sse_watch_heartbeat_is_silent() {
        let mut app = make_app_default();
        let (state, tx) = make_sse_state_pair();
        app.watch_sse = Some(state);

        // Only heartbeat — channel still live.
        tx.send(SseWatchMsg::Heartbeat).unwrap();
        // Drain; no new content should be reported.
        let _ = app.drain_sse_watch();

        let sse = app.watch_sse.as_ref().unwrap();
        assert!(sse.lines.is_empty());
        assert!(!sse.done);
        assert_eq!(sse.fail_count, 0);
    }

    #[test]
    fn drain_sse_watch_reconnects_on_first_error() {
        let mut app = make_app_default();
        let (state, tx) = make_sse_state_pair();
        app.watch_sse = Some(state);

        // One error — should schedule reconnect but not set done.
        tx.send(SseWatchMsg::Error("connection refused".to_string())).unwrap();
        let changed = app.drain_sse_watch();
        assert!(changed);

        let sse = app.watch_sse.as_ref().unwrap();
        assert_eq!(sse.fail_count, 1);
        assert!(!sse.done, "one error should not permanently stop reconnect");
        // A new rx should have been installed (fail_count < 3 → reconnect).
        // We can't inspect the new thread, but we can verify watch_sse is Some.
        assert!(app.watch_sse.is_some());
    }

    #[test]
    fn drain_sse_watch_stops_after_three_errors() {
        let mut app = make_app_default();
        let (state, _tx) = make_sse_state_pair(); // tx unused; rx gets replaced below
        app.watch_sse = Some(state);

        // First two errors — reconnect each time.
        for _ in 0..2 {
            // Re-fetch the sender for the new rx installed by reconnect.
            // Since we can't, we'll drive via Error on the *original* tx as
            // long as it's still connected. The reconnect installs a new rx,
            // but the old tx becomes orphaned. So subsequent drains on the
            // new rx will eventually see Disconnected (which also counts as
            // error). To keep the test simple, we manipulate fail_count
            // directly for errors 2 and 3.
            if let Some(sse) = &mut app.watch_sse {
                sse.fail_count += 1;
                sse.first_fail_at = Some(Instant::now());
            }
        }
        // Now fail_count = 2; send one more error to push it to 3.
        let (state2, tx2) = make_sse_state_pair();
        if let Some(sse) = &mut app.watch_sse {
            sse.rx = state2.rx;
            sse.fail_count = 2;
        }
        tx2.send(SseWatchMsg::Error("third failure".to_string())).unwrap();

        let changed = app.drain_sse_watch();
        assert!(changed);

        let sse = app.watch_sse.as_ref().unwrap();
        assert!(sse.done, "three errors should set done");
        assert_eq!(sse.fail_count, 3);
        // A toast should have been pushed.
        assert!(!app.toasts.is_empty(), "error toast should be pushed on fail limit");
    }

    #[test]
    fn drain_sse_watch_noop_when_done() {
        let mut app = make_app_default();
        let (mut state, tx) = make_sse_state_pair();
        state.done = true;
        app.watch_sse = Some(state);

        // Send lines — should be ignored because done=true.
        tx.send(SseWatchMsg::Lines { last_id: 10, text: "ignored".to_string() }).unwrap();
        let changed = app.drain_sse_watch();
        assert!(!changed, "done state should not trigger redraw");
        assert!(app.watch_sse.as_ref().unwrap().lines.is_empty());
    }

    #[test]
    fn close_watch_drops_sse_state() {
        let mut app = make_app_default();
        let (state, _tx) = make_sse_state_pair();
        app.watch = Some(WatchState {
            assignment_id: "x".to_string(),
            machine: "m".to_string(),
            repo: "r".to_string(),
            issue_number: 1,
            assignment_type: "work".to_string(),
            scroll: usize::MAX,
        });
        app.watch_sse = Some(state);

        app.close_watch();

        assert!(app.watch.is_none(), "watch should be cleared");
        assert!(app.watch_sse.is_none(), "watch_sse should be dropped on close");
    }

    #[test]
    fn watch_log_list_shows_connecting_when_lines_empty() {
        let mut app = make_app_default();
        app.watch = Some(WatchState {
            assignment_id: "abc".to_string(),
            machine: "remote".to_string(),
            repo: "myrepo".to_string(),
            issue_number: 42,
            assignment_type: "work".to_string(),
            scroll: usize::MAX,
        });
        let (state, _tx) = make_sse_state_pair();
        // No lines yet, not done → "Connecting…" placeholder.
        app.watch_sse = Some(state);

        let list = app.watch_log_list();
        let first_text: String = list.items.iter()
            .flat_map(|i| i.text.spans.iter().map(|s| s.text.clone()))
            .collect::<Vec<_>>()
            .join("");
        assert!(first_text.contains("Connecting"), "got: {}", first_text);
    }

    #[test]
    fn watch_log_list_shows_stream_ended_when_done() {
        let mut app = make_app_default();
        app.watch = Some(WatchState {
            assignment_id: "abc".to_string(),
            machine: "remote".to_string(),
            repo: "myrepo".to_string(),
            issue_number: 42,
            assignment_type: "work".to_string(),
            scroll: usize::MAX,
        });
        let (mut state, _tx) = make_sse_state_pair();
        state.lines.push("STATUS: done → done → confidence: high".to_string());
        state.done = true;
        app.watch_sse = Some(state);

        let list = app.watch_log_list();
        let all_text: String = list.items.iter()
            .flat_map(|i| i.text.spans.iter().map(|s| s.text.clone()))
            .collect::<Vec<_>>()
            .join(" ");
        assert!(all_text.contains("stream ended"), "got: {}", all_text);
    }

    #[test]
    fn watch_log_list_title_includes_refresh_hint() {
        let mut app = make_app_default();
        app.watch = Some(WatchState {
            assignment_id: "abc".to_string(),
            machine: "remote".to_string(),
            repo: "myrepo".to_string(),
            issue_number: 42,
            assignment_type: "work".to_string(),
            scroll: usize::MAX,
        });
        let (state, _tx) = make_sse_state_pair();
        app.watch_sse = Some(state);

        let list = app.watch_log_list();
        let title = list.title.as_ref().map(|t| {
            t.spans.iter().map(|s| s.text.clone()).collect::<String>()
        }).unwrap_or_default();
        assert!(title.contains("R=refresh"), "title should show R=refresh hint, got: {}", title);
    }

    // ── Settings: apply_settings_event ────────────────────────────────────────

    #[test]
    fn apply_settings_event_theme_mutates_and_returns_true() {
        use crate::settings::Theme;
        let mut app = make_app_default();
        app.settings.theme = Theme::Dark;
        let ev = FormEvent::SegmentedControlChanged {
            id: WidgetId::new("settings:theme"),
            selected_idx: Theme::Light.to_idx(),
        };
        let changed = app.apply_settings_event(&ev);
        assert!(changed, "should return true on theme change");
        assert_eq!(app.settings.theme, Theme::Light);
    }

    #[test]
    fn apply_settings_event_cadence_mutates_and_returns_true() {
        use crate::settings::RefreshCadence;
        let mut app = make_app_default();
        let ev = FormEvent::SegmentedControlChanged {
            id: WidgetId::new("settings:cadence"),
            selected_idx: RefreshCadence::ThirtySec.to_idx(),
        };
        assert!(app.apply_settings_event(&ev));
        assert_eq!(app.settings.refresh_cadence, RefreshCadence::ThirtySec);
    }

    #[test]
    fn apply_settings_event_log_ttl_mutates_and_returns_true() {
        use crate::settings::LogCacheTtl;
        let mut app = make_app_default();
        let ev = FormEvent::SegmentedControlChanged {
            id: WidgetId::new("settings:log-ttl"),
            selected_idx: LogCacheTtl::FiveSec.to_idx(),
        };
        assert!(app.apply_settings_event(&ev));
        assert_eq!(app.settings.log_cache_ttl, LogCacheTtl::FiveSec);
    }

    #[test]
    fn apply_settings_event_audio_toggle_mutates_and_returns_true() {
        let mut app = make_app_default();
        assert!(!app.settings.audio_on_completion);
        let ev = FormEvent::ToggleChanged {
            id: WidgetId::new("settings:audio"),
            value: true,
        };
        assert!(app.apply_settings_event(&ev));
        assert!(app.settings.audio_on_completion);
    }

    #[test]
    fn apply_settings_event_machine_model_mutates_and_returns_true() {
        use crate::settings::ModelPref;
        let mut app = make_app_default();
        let ev = FormEvent::SegmentedControlChanged {
            id: WidgetId::new("settings:model:mybox"),
            selected_idx: ModelPref::Opus.to_idx(),
        };
        assert!(app.apply_settings_event(&ev));
        assert_eq!(app.settings.machine_model.get("mybox"), Some(&ModelPref::Opus));
    }

    #[test]
    fn apply_settings_event_unknown_id_returns_false() {
        let mut app = make_app_default();
        let ev = FormEvent::SegmentedControlChanged {
            id: WidgetId::new("settings:nonexistent"),
            selected_idx: 0,
        };
        assert!(!app.apply_settings_event(&ev), "unknown ID should return false");
    }

    #[test]
    fn apply_settings_event_unknown_toggle_id_returns_false() {
        let mut app = make_app_default();
        let ev = FormEvent::ToggleChanged {
            id: WidgetId::new("settings:not-audio"),
            value: true,
        };
        assert!(!app.apply_settings_event(&ev), "unknown toggle ID should return false");
    }

    // ── Settings: settings_change_focused ─────────────────────────────────────

    #[test]
    fn settings_change_focused_wraps_forward_from_last_to_first() {
        use crate::settings::Theme;
        // #237: settings is one unified form — Theme is the first
        // interactive field.
        let mut app = make_app_default();
        app.settings.theme = Theme::HighContrast; // last option (idx 2)
        app.settings_field_sel = 0; // the theme field is the first interactive field

        // Forward from last option → wraps to first.
        let changed = app.settings_change_focused(1);
        assert!(changed, "should change when wrapping");
        assert_eq!(app.settings.theme, Theme::Dark, "should wrap from HighContrast → Dark");
    }

    #[test]
    fn settings_change_focused_wraps_backward_from_first_to_last() {
        use crate::settings::Theme;
        let mut app = make_app_default();
        app.settings.theme = Theme::Dark; // first option (idx 0)
        app.settings_field_sel = 0;

        // Backward from first option → wraps to last.
        let changed = app.settings_change_focused(-1);
        assert!(changed, "should change when wrapping backward");
        assert_eq!(app.settings.theme, Theme::HighContrast, "should wrap from Dark → HighContrast");
    }

    // ── #237: unified single-form layout ─────────────────────────────────

    #[test]
    fn settings_form_contains_all_section_headers() {
        // After #237, all categories collapse into one form with Label
        // section headers between groups.  The user scrolls instead of
        // clicking categories on a sidebar nav.
        let app = make_app_default();
        let form = app.build_settings_form();
        let label_texts: Vec<String> = form
            .fields
            .iter()
            .filter(|f| matches!(f.kind, FieldKind::Label))
            .map(|f| f.label.spans.iter().map(|s| s.text.as_str())
                .collect::<String>())
            .collect();
        // Every previously-separate category must show up as a header.
        for expected in &[
            "Display",
            "Auto-Refresh",
            "Notifications",
            "Watch Overlay",
            "Per-Machine Model Overrides",
        ] {
            assert!(
                label_texts.iter().any(|t: &String| t.contains(expected)),
                "missing section header {:?} — got {:?}",
                expected,
                label_texts,
            );
        }
    }

    #[test]
    fn settings_form_first_interactive_field_is_theme() {
        // The "Display" section is first, so its Theme SegmentedControl
        // is the first interactive field — settings_field_sel=0 must
        // resolve to it.  Anchors the settings_change_focused_* tests.
        let app = make_app_default();
        let ids = app.settings_interactive_field_ids();
        assert!(!ids.is_empty());
        assert_eq!(ids[0].as_str(), "settings:theme");
    }

    // ── #271 part 2: persistent build status + test guidance ────────────

    #[test]
    fn test_guidance_not_shown_when_work_not_done_and_no_build() {
        // No Work assignment + no build record + not actionable → no
        // Test guidance block (would just be noise on a fresh issue).
        let app = make_pipeline_app();
        let summary = app.pipeline_issue_summary();
        let texts: Vec<String> = summary
            .items
            .iter()
            .map(|i| {
                i.text
                    .spans
                    .iter()
                    .map(|s| s.text.clone())
                    .collect::<String>()
            })
            .collect();
        assert!(
            !texts.iter().any(|t| t.contains("ready for manual verification")),
            "test guidance must not appear when there's no Work yet; got rows {:?}",
            texts,
        );
    }

    #[test]
    fn test_guidance_shows_persisted_success_after_build_completes() {
        // After a successful Phase 1 build the result is persisted on
        // `last_test_builds`; the Pipeline summary surfaces "✓
        // succeeded in Ns" so the user can see the outcome long after
        // the 4-second toast expired.
        let mut app = make_pipeline_app();
        // Set up a done Work assignment so the Test stage is actionable.
        let mut work = _stage_assignment("w1", "work", 100.0, "done");
        work.branch = Some("issue-42-fix".to_string());
        app.data.assignments.push(work);
        // Persist a successful build outcome for that work id.
        app.last_test_builds.insert(
            "w1".to_string(),
            TestBuildResult {
                branch: "issue-42-fix".to_string(),
                issue_number: 42,
                exit_code: 0,
                first_error: String::new(),
                log_path: std::path::PathBuf::from("/tmp/test-build-w1.log"),
                duration_secs: 150,
                finished_at: Instant::now(),
            },
        );

        let summary = app.pipeline_issue_summary();
        let texts: Vec<String> = summary
            .items
            .iter()
            .map(|i| {
                i.text
                    .spans
                    .iter()
                    .map(|s| s.text.clone())
                    .collect::<String>()
            })
            .collect();
        let joined = texts.join("\n");
        assert!(
            joined.contains("✓ succeeded"),
            "persisted success not surfaced; got {:?}",
            texts,
        );
        assert!(
            joined.contains("150s"),
            "build duration not surfaced; got {:?}",
            texts,
        );
    }

    #[test]
    fn test_guidance_shows_persisted_failure_with_log_path() {
        let mut app = make_pipeline_app();
        let mut work = _stage_assignment("w2", "work", 100.0, "done");
        work.branch = Some("issue-42-fix".to_string());
        app.data.assignments.push(work);
        app.last_test_builds.insert(
            "w2".to_string(),
            TestBuildResult {
                branch: "issue-42-fix".to_string(),
                issue_number: 42,
                exit_code: 101,
                first_error: "error[E0432]: unresolved import `foo`".to_string(),
                log_path: std::path::PathBuf::from("/tmp/test-build-w2.log"),
                duration_secs: 12,
                finished_at: Instant::now(),
            },
        );

        let summary = app.pipeline_issue_summary();
        let texts: Vec<String> = summary
            .items
            .iter()
            .map(|i| i.text.spans.iter().map(|s| s.text.clone()).collect::<String>())
            .collect();
        let joined = texts.join("\n");
        assert!(joined.contains("✗ exit 101"), "exit code missing; got {:?}", texts);
        assert!(joined.contains("/tmp/test-build-w2.log"));
        // Failure snippet surfaced.
        assert!(joined.contains("E0432"));
    }

    #[test]
    fn test_guidance_surfaces_pr_title_and_body_from_cache() {
        // The follow-up: when a PR exists and the cache has its title
        // / body / files, the Test guidance block surfaces them inline
        // so the user doesn't have to ask Claude what the worker did.
        let mut app = make_pipeline_app();
        let mut work = _stage_assignment("w1", "work", 100.0, "done");
        work.branch = Some("issue-42-fix".to_string());
        app.data.assignments.push(work);
        // Trigger the guidance block by recording a build outcome —
        // the test gate isn't actionable in the default fixture (no
        // "test" gate configured), but a persisted build record opens
        // the block too.
        app.last_test_builds.insert(
            "w1".to_string(),
            TestBuildResult {
                branch: "issue-42-fix".to_string(),
                issue_number: 42,
                exit_code: 0,
                first_error: String::new(),
                log_path: std::path::PathBuf::from("/tmp/test-build-w1.log"),
                duration_secs: 150,
                finished_at: Instant::now(),
            },
        );
        // Plant a merge-queue entry so `pipeline_pr_number` resolves.
        app.data.merge_queue.push(MergeQueueEntry {
            assignment_id: "w1".to_string(),
            issue_number: Some(42),
            state: "pending".to_string(),
            pr_number: Some(123),
            pr_url: Some("https://github.com/acme/api/pull/123".to_string()),
            repo_github: "acme/api".to_string(),
        });
        // Seed the cache directly — bypasses the gh subprocess.
        app.fetched_prs_cache.borrow_mut().insert(
            ("acme/api".to_string(), 123),
            FetchedPr {
                title: "Add folder picker primitive".to_string(),
                body: "Added a new sample app at examples/folder_picker_demo.rs.\n\nRun `cargo run --example folder_picker_demo` to verify.".to_string(),
                files: vec![
                    "src/folder_picker.rs".to_string(),
                    "examples/folder_picker_demo.rs".to_string(),
                ],
                reviews: Vec::new(),
            },
        );

        let summary = app.pipeline_issue_summary();
        let joined: String = summary
            .items
            .iter()
            .flat_map(|i| i.text.spans.iter().map(|s| s.text.as_str()))
            .collect::<Vec<&str>>()
            .join("\n");

        assert!(joined.contains("Add folder picker primitive"), "PR title missing: {joined}");
        assert!(
            joined.contains("examples/folder_picker_demo.rs"),
            "sample-app file path missing: {joined}",
        );
        assert!(joined.contains("#123"), "PR number missing");
        assert!(joined.contains("(2 changed)"), "files-changed count missing");
    }

    #[test]
    fn test_guidance_surfaces_latest_review_verdict_and_body() {
        // The user's #166 gripe: Review stage is green but the review
        // body isn't visible.  With the gh-pr fetch including reviews,
        // the latest substantive review surfaces inline with its
        // verdict (Approved / Changes Requested) and body preview.
        let mut app = make_pipeline_app();
        let mut work = _stage_assignment("w1", "work", 100.0, "done");
        work.branch = Some("issue-166-folder-picker".to_string());
        app.data.assignments.push(work);
        // Build record opens the guidance block.
        app.last_test_builds.insert(
            "w1".to_string(),
            TestBuildResult {
                branch: "issue-166-folder-picker".to_string(),
                issue_number: 166,
                exit_code: 0,
                first_error: String::new(),
                log_path: std::path::PathBuf::from("/tmp/test-build-w1.log"),
                duration_secs: 22,
                finished_at: Instant::now(),
            },
        );
        // Merge queue entry → resolves PR number.
        app.data.merge_queue.push(MergeQueueEntry {
            assignment_id: "w1".to_string(),
            issue_number: Some(42),
            state: "pending".to_string(),
            pr_number: Some(243),
            pr_url: Some("https://github.com/acme/api/pull/243".to_string()),
            repo_github: "acme/api".to_string(),
        });
        // Seed the cache with a fully populated PR including review.
        app.fetched_prs_cache.borrow_mut().insert(
            ("acme/api".to_string(), 243),
            FetchedPr {
                title: "Folder picker primitive".to_string(),
                body: "Added examples/folder_picker_demo.rs.".to_string(),
                files: vec!["src/folder_picker.rs".to_string()],
                reviews: vec![
                    // Older drive-by comment that should be ignored.
                    FetchedReview {
                        state: "COMMENTED".to_string(),
                        body: String::new(),
                    },
                    // The substantive approval — what the user wants
                    // to read to decide if it's safe to merge.
                    FetchedReview {
                        state: "APPROVED".to_string(),
                        body: "Reviewed the diff; all CLAUDE.md rules respected. Tests pass locally. Merge when ready.".to_string(),
                    },
                ],
            },
        );

        let summary = app.pipeline_issue_summary();
        let joined: String = summary
            .items
            .iter()
            .flat_map(|i| i.text.spans.iter().map(|s| s.text.as_str()))
            .collect::<Vec<&str>>()
            .join("\n");

        // The verdict surfaces with the green ✓ Approved.
        assert!(joined.contains("✓ Approved"), "approve verdict missing: {joined}");
        // The body text surfaces so the user can decide if it's safe
        // to merge without clicking out to GitHub.
        assert!(
            joined.contains("CLAUDE.md rules respected"),
            "review body text missing: {joined}",
        );
    }

    #[test]
    fn test_guidance_surfaces_changes_requested_review() {
        // Symmetric test: the CHANGES_REQUESTED branch surfaces a
        // red ✗ verdict with the reviewer's body text.
        let mut app = make_pipeline_app();
        let mut work = _stage_assignment("w2", "work", 100.0, "done");
        work.branch = Some("issue-200-fix".to_string());
        app.data.assignments.push(work);
        app.last_test_builds.insert(
            "w2".to_string(),
            TestBuildResult {
                branch: "issue-200-fix".to_string(),
                issue_number: 200,
                exit_code: 0,
                first_error: String::new(),
                log_path: std::path::PathBuf::from("/tmp/test-build-w2.log"),
                duration_secs: 5,
                finished_at: Instant::now(),
            },
        );
        app.data.merge_queue.push(MergeQueueEntry {
            assignment_id: "w2".to_string(),
            issue_number: Some(42),
            state: "pending".to_string(),
            pr_number: Some(244),
            pr_url: None,
            repo_github: "acme/api".to_string(),
        });
        app.fetched_prs_cache.borrow_mut().insert(
            ("acme/api".to_string(), 244),
            FetchedPr {
                title: "WIP fix".to_string(),
                body: String::new(),
                files: vec!["src/lib.rs".to_string()],
                reviews: vec![FetchedReview {
                    state: "CHANGES_REQUESTED".to_string(),
                    body: "src/lib.rs:42 — missing null check; add a guard before deref.".to_string(),
                }],
            },
        );

        let summary = app.pipeline_issue_summary();
        let joined: String = summary
            .items
            .iter()
            .flat_map(|i| i.text.spans.iter().map(|s| s.text.as_str()))
            .collect::<Vec<&str>>()
            .join("\n");

        assert!(joined.contains("✗ Changes Requested"));
        assert!(joined.contains("missing null check"));
    }

    #[test]
    fn test_guidance_shows_loading_when_pr_cache_empty() {
        // Cache miss → the rendering should show a "loading…" hint
        // rather than blowing up.  The actual fetch is kicked off by
        // `pr_info_for_issue`; tests don't shell out to gh.
        let mut app = make_pipeline_app();
        let mut work = _stage_assignment("w2", "work", 100.0, "done");
        work.branch = Some("issue-42-fix".to_string());
        app.data.assignments.push(work);
        // Open the guidance block via a persisted build record (no
        // test gate in this fixture; build record opens the block).
        app.last_test_builds.insert(
            "w2".to_string(),
            TestBuildResult {
                branch: "issue-42-fix".to_string(),
                issue_number: 42,
                exit_code: 0,
                first_error: String::new(),
                log_path: std::path::PathBuf::from("/tmp/test-build-w2.log"),
                duration_secs: 1,
                finished_at: Instant::now(),
            },
        );
        app.data.merge_queue.push(MergeQueueEntry {
            assignment_id: "w2".to_string(),
            issue_number: Some(42),
            state: "pending".to_string(),
            pr_number: Some(124),
            pr_url: None,
            repo_github: "acme/api".to_string(),
        });
        // Pre-populate `pending_pr_fetches` so `pr_info_for_issue`
        // doesn't actually shell out to gh during the test.  An empty
        // tx-half is fine; the cache stays empty and we just exercise
        // the "loading" render path.
        let (_tx, rx) = std::sync::mpsc::channel::<Result<FetchedPr, String>>();
        app.pending_pr_fetches
            .borrow_mut()
            .insert(("acme/api".to_string(), 124), rx);

        let summary = app.pipeline_issue_summary();
        let joined: String = summary
            .items
            .iter()
            .flat_map(|i| i.text.spans.iter().map(|s| s.text.as_str()))
            .collect::<Vec<&str>>()
            .join("\n");
        assert!(joined.contains("loading"), "loading hint missing: {joined}");
    }

    #[test]
    fn test_guidance_hidden_when_no_persisted_build_and_test_not_actionable() {
        // An issue with no Work and no build record gets no test row.
        let app = make_pipeline_app();
        let summary = app.pipeline_issue_summary();
        for it in &summary.items {
            let text: String = it.text.spans.iter().map(|s| s.text.clone()).collect();
            assert!(
                !text.contains("(not run yet — press B)"),
                "stub build row leaked into a non-actionable issue: {:?}",
                text,
            );
        }
    }

    // ── #270: selected-item action bar (no-mouse / SSH-friendly) ────────

    #[test]
    fn sidebar_action_bar_none_when_no_row_selected() {
        // Without a focused row there's no row-specific verb to surface.
        let app = make_app_default();
        assert!(app.target_for_selected_sidebar_row().is_none());
        assert!(app.sidebar_action_bar().is_none());
    }

    #[test]
    fn sidebar_action_bar_offers_refine_on_backlog_row() {
        let mut app = make_app_default();
        // Seed a Backlog issue and focus it on the Board sidebar.
        app.data.open_issues.push(OpenIssue {
            repo_name: "repo-a".to_string(),
            number: 88,
            title: "backlog item".to_string(),
            body: String::new(),
            state: "open".to_string(),
            labels: Vec::new(),
        });
        app.rebuild_board_sidebar();
        app.select_issue("repo-a", 88);

        let bar = app.sidebar_action_bar().expect("Backlog selection → action bar");
        let labels = toolbar_action_labels(&bar);
        assert!(
            labels.iter().any(|l| l.contains("Refine")),
            "expected Refine button on a Backlog row; got {:?}",
            labels,
        );
        // The action bar must NOT carry Copy or Refresh — those aren't
        // row-state-specific (they live in the right-click menu only).
        assert!(!labels.iter().any(|l| l.contains("Copy")));
        assert!(!labels.iter().any(|l| l.contains("Refresh")));
    }

    #[test]
    fn sidebar_action_bar_offers_mark_refined_on_refining_row() {
        let mut app = make_app_default();
        app.data.open_issues.push(OpenIssue {
            repo_name: "repo-a".to_string(),
            number: 89,
            title: "refining item".to_string(),
            body: String::new(),
            state: "open".to_string(),
            labels: vec!["status:refining".to_string()],
        });
        app.rebuild_board_sidebar();
        app.select_issue("repo-a", 89);

        let bar = app.sidebar_action_bar().expect("Refining selection → action bar");
        let labels = toolbar_action_labels(&bar);
        assert!(labels.iter().any(|l| l.contains("Mark Refined")));
        assert!(labels.iter().any(|l| l.contains("Drop to Backlog")));
    }

    #[test]
    fn sidebar_action_bar_segments_carry_sidebar_action_prefix_for_dispatch() {
        // The hit-tester strips `sidebar-action:` to recover the
        // action_id; segments must carry the prefix so right-click
        // and bar dispatch can be told apart from the same code path.
        let mut app = make_app_default();
        app.data.open_issues.push(OpenIssue {
            repo_name: "repo-a".to_string(),
            number: 90,
            title: "t".to_string(),
            body: String::new(),
            state: "open".to_string(),
            labels: Vec::new(),
        });
        app.rebuild_board_sidebar();
        app.select_issue("repo-a", 90);

        let bar = app.sidebar_action_bar().expect("backlog bar");
        let ids = toolbar_action_ids(&bar);
        assert!(
            ids.iter().all(|id| id.starts_with("sidebar-action:")),
            "every clickable action must carry the sidebar-action prefix; got {:?}",
            ids,
        );
    }

    // ── #249 Principle 1: panel toolbar (clickable verb buttons) ────────

    #[test]
    fn panel_toolbar_board_contains_core_verbs() {
        // #192 / #263: Board toolbar now Notify · Retry · Purge.
        // Plan / Approve / Merge dropped (Plan & Approve retired with
        // Proposals; Merge is Pipeline-only).
        let app = make_app_default();
        let bar = app.panel_toolbar().expect("Board has a toolbar");
        let labels = toolbar_action_labels(&bar);
        for expected in &["[N]otify", "[R]etry", "[P]urge"] {
            assert!(
                labels.iter().any(|l| l.contains(expected)),
                "Board toolbar missing {:?}; got {:?}",
                expected,
                labels,
            );
        }
        // Plan / Approve / Merge must NOT appear on the Board toolbar.
        for unexpected in &["[P]lan", "[A]pprove", "[M]erge"] {
            assert!(
                !labels.iter().any(|l| l.contains(unexpected)),
                "Board toolbar should not include {:?}; got {:?}",
                unexpected,
                labels,
            );
        }
    }

    #[test]
    fn panel_toolbar_pipeline_contains_pipeline_verbs() {
        // #192 / #263: Pipeline toolbar now Notify · Ready · Merge · Retry.
        // Plan dropped — Plan-stage rendering is per-issue (#258), not
        // a panel-wide verb.
        let app = make_pipeline_app();
        let mut app = app;
        app.active_view = SidebarView::Pipeline;
        let bar = app.panel_toolbar().expect("Pipeline has a toolbar");
        let labels = toolbar_action_labels(&bar);
        for expected in &["[N]otify", "[r]eady", "[M]erge", "[R]etry"] {
            assert!(
                labels.iter().any(|l| l.contains(expected)),
                "Pipeline toolbar missing {:?}; got {:?}",
                expected,
                labels,
            );
        }
        assert!(
            !labels.iter().any(|l| l.contains("[P]lan")),
            "Pipeline toolbar should not include [P]lan; got {:?}",
            labels,
        );
    }

    #[test]
    fn panel_toolbar_settings_and_machines_have_no_toolbar() {
        // Settings and Machines have no panel-level verbs — toolbar
        // should be absent so the form / detail list expands to fill the
        // full main panel.
        let mut app = make_app_default();
        app.active_view = SidebarView::Settings;
        assert!(app.panel_toolbar().is_none());
        app.active_view = SidebarView::Machines;
        assert!(app.panel_toolbar().is_none());
    }

    #[test]
    fn panel_toolbar_disabled_button_keeps_id_but_marks_disabled() {
        // The Retry button on the Board panel is disabled when there's
        // no failed assignment to retry.  Under #272 the toolbar
        // primitive carries first-class `enabled: false` — the button
        // still has its id (for hover tooltips) but the layout's
        // `hit_test` declines clicks.  This is the behavioural upgrade
        // over the old "drop the action_id" workaround.
        let app = make_app_default();
        let bar = app.panel_toolbar().expect("Board toolbar");
        let retry = bar
            .buttons
            .iter()
            .find_map(|b| match b {
                ToolbarButton::Action { id, label, enabled, .. }
                    if label.contains("[R]etry") =>
                {
                    Some((id.clone(), *enabled))
                }
                _ => None,
            })
            .expect("Retry button present");
        assert!(!retry.1, "Retry should be disabled (no failed assignments)");
        // Layout records the button so hover state can fire, but the
        // hit-test must refuse clicks landing on it.
        let layout = bar.layout(0.0, 0.0, 200.0, 1.0, toolbar_tui_measure);
        let visible = layout
            .visible_items
            .iter()
            .find(|v| v.action_id.as_ref() == Some(&retry.0))
            .expect("Retry visible entry");
        assert!(!visible.clickable, "Disabled action must not be clickable");
    }

    #[test]
    fn panel_toolbar_suppressed_while_pending_confirm() {
        // During an inline confirm prompt (purge/force-merge/test-fail),
        // the toolbar is hidden — those modes consume every key and a
        // visible toolbar above them would be misleading.
        let mut app = make_app_default();
        assert!(app.panel_toolbar().is_some());
        app.pending_purge = Some((1, 0));
        assert!(app.panel_toolbar().is_none());
        app.pending_purge = None;
        app.pending_force_merge = Some("api".to_string());
        assert!(app.panel_toolbar().is_none());
    }

    #[test]
    fn dispatch_toolbar_action_approve_no_longer_recognised() {
        // #192 / #263: `toolbar:approve` is retired alongside the
        // PROPOSALS section.  Stale segments (from a long-running
        // session) fall through to the catch-all branch — returns
        // false, no side effects, no spurious `coord approve` spawn.
        let mut app = make_app_default();
        let before = app.toasts.len();
        let result = app.dispatch_toolbar_action("toolbar:approve");
        assert!(!result);
        // No toast — the catch-all is silent so the user isn't spammed
        // when an unrelated stale segment is clicked.
        assert_eq!(app.toasts.len(), before);
    }

    #[test]
    fn dispatch_toolbar_action_ignores_unknown_id() {
        // Defence-in-depth: an unknown action_id (stale segment from a
        // previous frame) must not panic or fire side effects.
        let mut app = make_app_default();
        let before = app.toasts.len();
        let result = app.dispatch_toolbar_action("toolbar:bogus");
        assert!(!result);
        assert_eq!(app.toasts.len(), before);
    }

    // ── #259: right-click context menu primitive ────────────────────────

    /// Test helper: build a `BoardRow` target with sensible defaults so
    /// each call site doesn't have to spell out every field.
    fn board_target(
        issue_number: Option<u64>,
        lifecycle: BoardRowLifecycle,
    ) -> ContextMenuTarget {
        ContextMenuTarget::BoardRow {
            issue_number,
            repo_name: Some("repo-a".to_string()),
            lifecycle,
        }
    }

    fn pipeline_target(issue_number: Option<u64>) -> ContextMenuTarget {
        ContextMenuTarget::PipelineRow {
            issue_number,
            lifecycle: PipelineRowLifecycle::Other,
        }
    }

    #[test]
    fn open_context_menu_board_row_seeds_state() {
        // Opening on a Board row produces a non-empty item list and
        // picks the first selectable item as the keyboard focus.
        let mut app = make_app_default();
        let pos = Point::new(10.0, 5.0);
        let opened = app.open_context_menu(
            pos,
            board_target(Some(42), BoardRowLifecycle::InFlight),
        );
        assert!(opened, "context menu should open for a Board row");
        let state = app.pending_context_menu.expect("state set");
        assert!(!state.items.is_empty());
        // First action item is the keyboard focus.  In-flight rows
        // don't get a Refine, so Copy is first.
        assert_eq!(
            state.items[state.selected_idx].action_id.as_deref(),
            Some("copy-issue-number"),
        );
        assert!(state.items.iter().any(|it| it.label.contains("#42")));
    }

    #[test]
    fn open_context_menu_includes_refresh_for_pipeline_row() {
        let mut app = make_app_default();
        let opened = app.open_context_menu(Point::new(1.0, 1.0), pipeline_target(None));
        assert!(opened);
        let state = app.pending_context_menu.expect("state set");
        assert!(state.items.iter().any(|it| it.label == "Refresh"));
    }

    #[test]
    fn context_menu_move_selection_skips_separators() {
        let mut app = make_app_default();
        app.open_context_menu(
            Point::new(0.0, 0.0),
            board_target(Some(1), BoardRowLifecycle::InFlight),
        );
        // Layout (Copy / separator / Refresh) — move forward should land
        // on Refresh, not the separator.
        let state_before = app.pending_context_menu.clone().unwrap();
        app.context_menu_move_selection(1);
        let state_after = app.pending_context_menu.clone().unwrap();
        assert_ne!(state_before.selected_idx, state_after.selected_idx);
        assert_eq!(
            state_after.items[state_after.selected_idx].action_id.as_deref(),
            Some("refresh"),
        );
    }

    #[test]
    fn context_menu_activate_fires_action_and_dismisses() {
        let mut app = make_app_default();
        app.open_context_menu(
            Point::new(0.0, 0.0),
            board_target(Some(7), BoardRowLifecycle::InFlight),
        );
        let before = app.toasts.len();
        let fired = app.context_menu_activate_selected();
        assert!(fired);
        // Menu dismissed.
        assert!(app.pending_context_menu.is_none());
        // Copy is a stub that pushes a toast.
        assert_eq!(app.toasts.len(), before + 1);
    }

    #[test]
    fn context_menu_dismiss_clears_state() {
        let mut app = make_app_default();
        app.open_context_menu(
            Point::new(0.0, 0.0),
            board_target(None, BoardRowLifecycle::Unknown),
        );
        assert!(app.pending_context_menu.is_some());
        app.dismiss_context_menu();
        assert!(app.pending_context_menu.is_none());
    }

    #[test]
    fn dispatch_context_menu_action_refresh_does_not_toast() {
        // Refresh is the only smoke-test item that runs real code (kicks
        // a background data load).  It should NOT push a toast.
        let mut app = make_app_default();
        let before = app.toasts.len();
        let target = board_target(None, BoardRowLifecycle::Unknown);
        let result = app.dispatch_context_menu_action("refresh", &target);
        assert!(result);
        assert_eq!(app.toasts.len(), before);
    }

    #[test]
    fn dispatch_context_menu_action_unknown_id_warns_via_toast() {
        // Defence-in-depth: a stale action id (e.g. from a previous frame)
        // must not panic — it surfaces a Warning toast instead.
        let mut app = make_app_default();
        let before = app.toasts.len();
        let target = board_target(None, BoardRowLifecycle::Unknown);
        let result = app.dispatch_context_menu_action("bogus", &target);
        assert!(!result);
        assert_eq!(app.toasts.len(), before + 1);
    }

    // ── #260: Refine right-click action ─────────────────────────────────

    #[test]
    fn backlog_row_context_menu_offers_refine() {
        // The Refine action is only meaningful for Backlog rows — it'd
        // be misleading on In-flight / Completed.
        let app = make_app_default();
        let items = app.context_menu_items_for_board_row(
            Some(42),
            &BoardRowLifecycle::Backlog,
        );
        assert!(
            items.iter().any(|it| it.action_id.as_deref() == Some("refine")),
            "Backlog menu must include Refine; got {:?}",
            items.iter().map(|it| &it.label).collect::<Vec<_>>(),
        );
    }

    #[test]
    fn in_flight_row_context_menu_omits_refine() {
        // Adding `status:refining` to an in-flight row would be
        // confusing — refining is upstream of dispatch.
        let app = make_app_default();
        let items = app.context_menu_items_for_board_row(
            Some(42),
            &BoardRowLifecycle::InFlight,
        );
        assert!(
            !items.iter().any(|it| it.action_id.as_deref() == Some("refine")),
            "Refine must not appear on In-flight rows",
        );
    }

    #[test]
    fn board_row_lifecycle_classifies_open_no_labels_as_backlog() {
        let mut app = make_app_default();
        app.data.open_issues.push(OpenIssue {
            repo_name: "repo-a".to_string(),
            number: 10,
            title: String::new(),
            body: String::new(),
            state: "open".to_string(),
            labels: Vec::new(),
        });
        assert_eq!(
            app.board_row_lifecycle("repo-a", 10),
            BoardRowLifecycle::Backlog,
        );
    }

    #[test]
    fn board_row_lifecycle_status_refining_label_yields_refining() {
        let mut app = make_app_default();
        app.data.open_issues.push(OpenIssue {
            repo_name: "repo-a".to_string(),
            number: 11,
            title: String::new(),
            body: String::new(),
            state: "open".to_string(),
            labels: vec!["status:refining".to_string()],
        });
        assert_eq!(
            app.board_row_lifecycle("repo-a", 11),
            BoardRowLifecycle::Refining,
        );
    }

    #[test]
    fn board_row_lifecycle_status_ready_label_yields_refined() {
        let mut app = make_app_default();
        app.data.open_issues.push(OpenIssue {
            repo_name: "repo-a".to_string(),
            number: 12,
            title: String::new(),
            body: String::new(),
            state: "open".to_string(),
            labels: vec!["status:ready".to_string()],
        });
        assert_eq!(
            app.board_row_lifecycle("repo-a", 12),
            BoardRowLifecycle::Refined,
        );
    }

    #[test]
    fn dispatch_refine_without_target_data_toasts_and_no_ops() {
        // Refine requires both an issue number and a repo name; without
        // either, the dispatch should surface guidance rather than
        // crashing on the missing data.
        let mut app = make_app_default();
        let before = app.toasts.len();
        let target = ContextMenuTarget::BoardRow {
            issue_number: None,
            repo_name: None,
            lifecycle: BoardRowLifecycle::Backlog,
        };
        let fired = app.dispatch_context_menu_action("refine", &target);
        assert!(!fired);
        assert_eq!(app.toasts.len(), before + 1);
    }

    // ── #261: Send to Pipeline right-click action ───────────────────────

    #[test]
    fn refined_row_context_menu_offers_send_to_pipeline() {
        // Send to Pipeline is the canonical Refined-row action.
        let app = make_app_default();
        let items = app.context_menu_items_for_board_row(
            Some(99),
            &BoardRowLifecycle::Refined,
        );
        assert!(
            items
                .iter()
                .any(|it| it.action_id.as_deref() == Some("send-to-pipeline")),
            "Refined menu must include Send to Pipeline; got {:?}",
            items.iter().map(|it| &it.label).collect::<Vec<_>>(),
        );
    }

    #[test]
    fn backlog_row_context_menu_omits_send_to_pipeline() {
        // Pre-Refined rows still need scoping work — sending them to
        // the Pipeline would dispatch undefined work.
        let app = make_app_default();
        let items = app.context_menu_items_for_board_row(
            Some(99),
            &BoardRowLifecycle::Backlog,
        );
        assert!(
            !items
                .iter()
                .any(|it| it.action_id.as_deref() == Some("send-to-pipeline")),
            "Send to Pipeline must not appear on Backlog rows",
        );
    }

    #[test]
    fn in_flight_row_context_menu_omits_send_to_pipeline() {
        // In-flight rows already carry the `coord` label — the action
        // would be a no-op (and confusing).
        let app = make_app_default();
        let items = app.context_menu_items_for_board_row(
            Some(99),
            &BoardRowLifecycle::InFlight,
        );
        assert!(
            !items
                .iter()
                .any(|it| it.action_id.as_deref() == Some("send-to-pipeline")),
            "Send to Pipeline must not appear on In-flight rows",
        );
    }

    #[test]
    fn dispatch_send_to_pipeline_without_target_data_toasts_and_no_ops() {
        let mut app = make_app_default();
        let before = app.toasts.len();
        let target = ContextMenuTarget::BoardRow {
            issue_number: None,
            repo_name: None,
            lifecycle: BoardRowLifecycle::Refined,
        };
        let fired = app.dispatch_context_menu_action("send-to-pipeline", &target);
        assert!(!fired);
        assert_eq!(app.toasts.len(), before + 1);
    }

    // ── #266: Refining row right-click actions ──────────────────────────

    #[test]
    fn refining_row_context_menu_offers_mark_refined_and_drop_to_backlog() {
        // Refining was a UX dead-end before #266 — users had to shell
        // out to `coord ready`.  Both directions are now in the menu.
        let app = make_app_default();
        let items = app.context_menu_items_for_board_row(
            Some(42),
            &BoardRowLifecycle::Refining,
        );
        let action_ids: Vec<&str> = items
            .iter()
            .filter_map(|it| it.action_id.as_deref())
            .collect();
        assert!(
            action_ids.contains(&"mark-refined"),
            "Refining menu must offer Mark Refined; got {:?}",
            action_ids,
        );
        assert!(
            action_ids.contains(&"drop-to-backlog"),
            "Refining menu must offer Drop to Backlog; got {:?}",
            action_ids,
        );
    }

    #[test]
    fn refined_row_context_menu_offers_drop_to_refining() {
        // Symmetric escape hatch — let the user re-open scoping on a
        // Refined row without dropping all the way to Backlog.
        let app = make_app_default();
        let items = app.context_menu_items_for_board_row(
            Some(42),
            &BoardRowLifecycle::Refined,
        );
        let action_ids: Vec<&str> = items
            .iter()
            .filter_map(|it| it.action_id.as_deref())
            .collect();
        assert!(
            action_ids.contains(&"drop-to-refining"),
            "Refined menu must offer Drop to Refining; got {:?}",
            action_ids,
        );
    }

    #[test]
    fn backlog_row_omits_refining_actions() {
        // Mark Refined / Drop to Backlog are state-transition actions
        // only meaningful for Refining rows.
        let app = make_app_default();
        let items = app.context_menu_items_for_board_row(
            Some(42),
            &BoardRowLifecycle::Backlog,
        );
        let action_ids: Vec<&str> = items
            .iter()
            .filter_map(|it| it.action_id.as_deref())
            .collect();
        assert!(!action_ids.contains(&"mark-refined"));
        assert!(!action_ids.contains(&"drop-to-backlog"));
    }

    #[test]
    fn dispatch_mark_refined_without_target_toasts_and_no_ops() {
        let mut app = make_app_default();
        let before = app.toasts.len();
        let target = ContextMenuTarget::BoardRow {
            issue_number: None,
            repo_name: None,
            lifecycle: BoardRowLifecycle::Refining,
        };
        let fired = app.dispatch_context_menu_action("mark-refined", &target);
        assert!(!fired);
        assert_eq!(app.toasts.len(), before + 1);
    }

    // ── #262: Start right-click on Pipeline:New rows ────────────────────

    #[test]
    fn pipeline_new_row_context_menu_offers_both_start_variants() {
        let app = make_app_default();
        let items = app.context_menu_items_for_pipeline_row(
            Some(42),
            &PipelineRowLifecycle::New,
        );
        let action_ids: Vec<&str> = items
            .iter()
            .filter_map(|it| it.action_id.as_deref())
            .collect();
        assert!(
            action_ids.contains(&"start-with-plan"),
            "Pipeline:New menu must offer Start with Plan; got {:?}",
            action_ids,
        );
        assert!(
            action_ids.contains(&"start-skip-plan"),
            "Pipeline:New menu must offer Skip Plan; got {:?}",
            action_ids,
        );
    }

    #[test]
    fn pipeline_in_progress_row_omits_start() {
        // Once work is dispatched, Start is meaningless — Retry / View
        // Log become the relevant actions (filed for future work).
        let app = make_app_default();
        let items = app.context_menu_items_for_pipeline_row(
            Some(42),
            &PipelineRowLifecycle::InProgress,
        );
        let action_ids: Vec<&str> = items
            .iter()
            .filter_map(|it| it.action_id.as_deref())
            .collect();
        assert!(!action_ids.contains(&"start-with-plan"));
        assert!(!action_ids.contains(&"start-skip-plan"));
    }

    #[test]
    fn pipeline_done_row_omits_start() {
        let app = make_app_default();
        let items = app.context_menu_items_for_pipeline_row(
            Some(42),
            &PipelineRowLifecycle::Done,
        );
        let action_ids: Vec<&str> = items
            .iter()
            .filter_map(|it| it.action_id.as_deref())
            .collect();
        assert!(!action_ids.contains(&"start-with-plan"));
        assert!(!action_ids.contains(&"start-skip-plan"));
    }

    #[test]
    fn pipeline_in_progress_row_offers_watch_and_stop() {
        // Closes the action-bar coverage gap that left In-progress rows
        // with an empty bar (only Copy + Refresh, both filtered out).
        let app = make_app_default();
        let items = app.context_menu_items_for_pipeline_row(
            Some(42),
            &PipelineRowLifecycle::InProgress,
        );
        let action_ids: Vec<&str> = items
            .iter()
            .filter_map(|it| it.action_id.as_deref())
            .collect();
        assert!(
            action_ids.contains(&"watch"),
            "Pipeline:InProgress menu must offer Watch; got {:?}",
            action_ids,
        );
        assert!(
            action_ids.contains(&"stop"),
            "Pipeline:InProgress menu must offer Stop; got {:?}",
            action_ids,
        );
    }

    #[test]
    fn pipeline_done_row_offers_open_pr() {
        let app = make_app_default();
        let items = app.context_menu_items_for_pipeline_row(
            Some(42),
            &PipelineRowLifecycle::Done,
        );
        let action_ids: Vec<&str> = items
            .iter()
            .filter_map(|it| it.action_id.as_deref())
            .collect();
        assert!(
            action_ids.contains(&"open-pr"),
            "Pipeline:Done menu must offer Open PR; got {:?}",
            action_ids,
        );
    }

    #[test]
    fn pipeline_row_offers_bounce_when_review_requested_changes() {
        // When a review came back as request-changes, the action bar
        // surfaces "Address review findings" so the user can dispatch
        // a fix worker without leaving the TUI.
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        // Work + paired review with request-changes.
        let mut work = _work_assignment("w1", 100.0, "done", None);
        work.issue_number = 42;
        app.data.assignments.push(work);
        let mut review = _stage_assignment("rev-w1", "review", 200.0, "done");
        review.issue_number = 42;
        review.review_of_assignment_id = Some("w1".to_string());
        review.review_verdict = Some("request-changes".to_string());
        app.data.assignments.push(review);

        let items = app.context_menu_items_for_pipeline_row(
            Some(42),
            &PipelineRowLifecycle::InProgress,
        );
        let action_ids: Vec<&str> = items
            .iter()
            .filter_map(|it| it.action_id.as_deref())
            .collect();
        assert!(
            action_ids.contains(&"bounce"),
            "InProgress row with request-changes review must offer 'bounce'; got {:?}",
            action_ids,
        );
    }

    #[test]
    fn pipeline_row_no_bounce_when_review_approved() {
        // Approved reviews shouldn't offer the bounce action — there's
        // nothing to fix.
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        let mut work = _work_assignment("w1", 100.0, "done", None);
        work.issue_number = 42;
        app.data.assignments.push(work);
        let mut review = _stage_assignment("rev-w1", "review", 200.0, "done");
        review.issue_number = 42;
        review.review_of_assignment_id = Some("w1".to_string());
        review.review_verdict = Some("approve".to_string());
        app.data.assignments.push(review);

        let items = app.context_menu_items_for_pipeline_row(
            Some(42),
            &PipelineRowLifecycle::InProgress,
        );
        let action_ids: Vec<&str> = items
            .iter()
            .filter_map(|it| it.action_id.as_deref())
            .collect();
        assert!(
            !action_ids.contains(&"bounce"),
            "approved review should not offer bounce; got {:?}",
            action_ids,
        );
    }

    #[test]
    fn dispatch_bounce_toasts_and_spawns_when_request_changes() {
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        // Seed merge_queue + work + request-changes review for issue 42.
        app.data.merge_queue.push(MergeQueueEntry {
            assignment_id: "w42".to_string(),
            issue_number: Some(42),
            state: "pending".to_string(),
            pr_number: Some(999),
            pr_url: None,
            repo_github: "acme/api".to_string(),
        });
        let mut review = _stage_assignment("rev-w42", "review", 200.0, "done");
        review.issue_number = 42;
        review.review_of_assignment_id = Some("w42".to_string());
        review.review_verdict = Some("request-changes".to_string());
        app.data.assignments.push(review);

        let toasts_before = app.toasts.len();
        let acted = app.dispatch_bounce_for_selected_pipeline_row();
        assert!(acted, "bounce must dispatch when a request-changes review exists");
        assert!(app.toasts.len() > toasts_before);
        let body = app.toasts.last().expect("toast").0.body.to_string();
        assert!(
            body.to_lowercase().contains("rev-w42") || body.to_lowercase().contains("fix"),
            "toast should mention dispatch; got {body:?}",
        );
    }

    #[test]
    fn dispatch_bounce_toasts_without_spawn_when_no_request_changes() {
        // No review pairing → bounce is a no-op with an explanatory toast.
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);

        let toasts_before = app.toasts.len();
        let acted = app.dispatch_bounce_for_selected_pipeline_row();
        assert!(!acted, "no spawn when nothing to bounce");
        assert!(app.toasts.len() > toasts_before);
    }

    #[test]
    fn pipeline_in_progress_row_action_bar_is_not_empty() {
        // Regression for the "I saw Refine on the board but didn't see
        // anything yet in pipelines" report: an in-flight Pipeline row
        // must produce a non-empty action bar.
        let mut app = make_pipeline_app();
        // Put issue #42 into InProgress by attaching a running work
        // assignment.
        app.data.assignments.push(_work_assignment("w1", 100.0, "running", None));
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);

        let bar = app
            .sidebar_action_bar()
            .expect("In-progress row must produce an action bar");
        let labels = toolbar_action_labels(&bar);
        assert!(
            labels.iter().any(|l| l == "Watch"),
            "expected Watch in action bar; got {:?}",
            labels,
        );
        assert!(
            labels.iter().any(|l| l == "Stop"),
            "expected Stop in action bar; got {:?}",
            labels,
        );
    }

    // ── #stage-content: per-stage focus + content panel ──────────────────

    #[test]
    fn focus_next_stage_advances_and_wraps() {
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        assert_eq!(app.pipeline_focused_stage, None);
        app.focus_next_pipeline_stage();
        assert_eq!(app.pipeline_focused_stage, Some(0));
        app.focus_next_pipeline_stage();
        assert_eq!(app.pipeline_focused_stage, Some(1));
        // Advance to the last + wrap to 0.
        let stage_count = app
            .pipeline_stage_names_for_issue(&app.pipeline_issues[0])
            .len();
        for _ in 2..stage_count {
            app.focus_next_pipeline_stage();
        }
        // Now focused on the last stage.  One more wraps back to 0.
        app.focus_next_pipeline_stage();
        assert_eq!(app.pipeline_focused_stage, Some(0));
    }

    #[test]
    fn focus_prev_stage_wraps_from_none_to_last() {
        // Pressing [ before anything is focused should snap to the
        // last stage (so users immediately see the most recent
        // pipeline output rather than the empty Plan stage).
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        let stage_count = app
            .pipeline_stage_names_for_issue(&app.pipeline_issues[0])
            .len();
        app.focus_prev_pipeline_stage();
        assert_eq!(app.pipeline_focused_stage, Some(stage_count - 1));
    }

    #[test]
    fn stage_content_review_renders_cached_findings() {
        // The DB cache lives on the review-typed assignment as a JSON
        // string.  stage_content_review should parse it and render
        // verdict + body.
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        let mut review = _stage_assignment("rev1", "review", 200.0, "done");
        review.issue_number = 42;
        review.review_of_assignment_id = Some("w1".to_string());
        review.review_verdict = Some("request-changes".to_string());
        review.review_findings = Some(
            "{\"verdict\":\"request-changes\",\"body\":\"## Issues\\n- Missing test for edge case\"}"
                .to_string(),
        );
        app.data.assignments.push(review);

        let issue = app.pipeline_issues[0].clone();
        let rows = app.stage_content_review(&issue);
        // Should contain the verdict label + the body content.
        let text: String = rows
            .iter()
            .flat_map(|r| r.text.spans.iter().map(|s| s.text.clone()))
            .collect::<Vec<_>>()
            .join(" ");
        assert!(
            text.contains("changes requested") || text.contains("request-changes"),
            "expected verdict in rendered content; got: {text:?}",
        );
        assert!(
            text.contains("Missing test for edge case"),
            "expected body in rendered content; got: {text:?}",
        );
    }

    #[test]
    fn stage_content_review_falls_back_when_no_cache() {
        let mut app = make_pipeline_app();
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);
        let mut review = _stage_assignment("rev2", "review", 200.0, "done");
        review.issue_number = 42;
        review.review_verdict = Some("approve".to_string());
        review.review_findings = None;  // no cache
        app.data.assignments.push(review);

        let rows = app.stage_content_review(&app.pipeline_issues[0].clone());
        let text: String = rows
            .iter()
            .flat_map(|r| r.text.spans.iter().map(|s| s.text.clone()))
            .collect::<Vec<_>>()
            .join(" ");
        assert!(
            text.contains("not yet parsed") || text.contains("coord notify"),
            "expected hint about parsing; got: {text:?}",
        );
    }

    #[test]
    fn pipeline_done_row_action_bar_offers_open_pr() {
        // pipeline_lifecycle_section classifies "Done" as is_closed=true,
        // so we close the issue + attach a merged PR to mirror the
        // post-merge state the user actually sees.
        let mut app = make_pipeline_app();
        app.pipeline_issues[0].is_closed = true;
        app.data.assignments.push(_work_assignment("w1", 100.0, "done", None));
        app.data.merge_queue.push(MergeQueueEntry {
            assignment_id: "w1".to_string(),
            issue_number: Some(42),
            state: "merged".to_string(),
            pr_number: Some(7),
            pr_url: None,
            repo_github: "acme/api".to_string(),
        });
        app.active_view = SidebarView::Pipeline;
        app.pipeline_sel = Some(0);

        let bar = app
            .sidebar_action_bar()
            .expect("Done row must produce an action bar");
        let labels = toolbar_action_labels(&bar);
        assert!(
            labels.iter().any(|l| l == "Open PR"),
            "expected Open PR in action bar; got {:?}",
            labels,
        );
    }

    #[test]
    fn pipeline_row_lifecycle_new_for_coord_status_ready_no_assignments() {
        // The motivating Pipeline:New definition: `coord` +
        // `status:ready`, no assignments.
        let mut app = make_pipeline_app();
        // Issue #42 in the fixture has coord_repo=Some("api"); add the
        // ready label to put it in the Pipeline:New bucket.
        app.pipeline_issues[0].all_labels.push("status:ready".to_string());
        let issue = app.pipeline_issues[0].clone();
        assert_eq!(
            app.pipeline_row_lifecycle(&issue),
            PipelineRowLifecycle::New,
        );
    }

    #[test]
    fn pipeline_row_lifecycle_in_progress_when_has_assignment() {
        let mut app = make_pipeline_app();
        app.data.assignments.push(_work_assignment("w1", 100.0, "running", None));
        let issue = app.pipeline_issues[0].clone();
        assert_eq!(
            app.pipeline_row_lifecycle(&issue),
            PipelineRowLifecycle::InProgress,
        );
    }

    #[test]
    fn pipeline_row_lifecycle_other_for_status_refining() {
        // A coord-labelled issue marked back for refinement is Other —
        // Start is not meaningful, the user should Mark Refined first.
        let mut app = make_pipeline_app();
        // #225 fixture seeds `status:ready` — replace with refining so
        // the classifier sees ONLY status:refining.
        app.pipeline_issues[0]
            .all_labels
            .retain(|l| !l.starts_with("status:"));
        app.pipeline_issues[0].all_labels.push("status:refining".to_string());
        let issue = app.pipeline_issues[0].clone();
        assert_eq!(
            app.pipeline_row_lifecycle(&issue),
            PipelineRowLifecycle::Other,
        );
    }

    #[test]
    fn dispatch_drop_to_backlog_without_target_toasts_and_no_ops() {
        let mut app = make_app_default();
        let before = app.toasts.len();
        let target = ContextMenuTarget::BoardRow {
            issue_number: None,
            repo_name: None,
            lifecycle: BoardRowLifecycle::Refining,
        };
        let fired = app.dispatch_context_menu_action("drop-to-backlog", &target);
        assert!(!fired);
        assert_eq!(app.toasts.len(), before + 1);
    }

    // ── #249 Principle 2: toast-on-no-op preconditions ──────────────────

    #[test]
    fn proposals_section_hidden_after_retirement() {
        // #192: PROPOSALS section is retired.  Even when the brain
        // has produced proposals (legacy `coord plan` users), the
        // sidebar should not render the section any more.  Right-
        // click → Send to Pipeline is the canonical path now.
        let mut app = make_app_default();
        // Seed a proposal as if `coord plan` ran.
        app.data.proposals.push(Proposal {
            id: 1,
            machine: "m".to_string(),
            repo: "api".to_string(),
            issue_number: 42,
            issue_title: "t".to_string(),
            rationale: "r".to_string(),
            proposal_type: "work".to_string(),
        });
        app.rebuild_board_sidebar();
        assert!(
            !app.has_proposals_section,
            "PROPOSALS section must be hidden after #192",
        );
        assert!(
            app.board_selected_proposal().is_none(),
            "no UI → no selectable proposal",
        );
    }

    #[test]
    fn no_op_toast_when_retry_pressed_without_failed_assignment() {
        // `R` in Board view requires a failed assignment to retry; on
        // an empty board the precondition is unmet and the toast path
        // fires.  Anchor the predicate that the handler keys off.
        let app = make_app_default();
        assert!(
            app.board_selected_failed_assignment().is_none(),
            "empty board ⇒ no failed assignment to retry",
        );
    }

    #[test]
    fn push_toast_appends_to_stack() {
        // Confirms the toast plumbing the no-op branches rely on.
        let mut app = make_app_default();
        let before = app.toasts.len();
        app.push_toast("title", "body", ToastSeverity::Info);
        assert_eq!(app.toasts.len(), before + 1);
    }

    #[test]
    fn settings_sidebar_placeholder_is_empty() {
        // The Settings sidebar shows just a header strip — no clickable
        // category rows — so misdirected clicks can't appear to do
        // something.
        let app = make_app_default();
        let list = app.settings_sidebar_placeholder();
        assert!(list.items.is_empty());
    }

    // ── #248: parse_coord_review_header ──────────────────────────────────

    #[test]
    fn parse_review_header_full_payload() {
        let body = "<!-- coord:review verdict=request-changes blocking=2 \
                    nonblocking=5 nits=1 reviewer=elitebook \
                    assignment=144ffa027a31 -->\n\n## Review\n\n…";
        let h = parse_coord_review_header(body).expect("header present");
        assert_eq!(h.verdict.as_deref(), Some("request-changes"));
        assert_eq!(h.blocking, Some(2));
        assert_eq!(h.nonblocking, Some(5));
        assert_eq!(h.nits, Some(1));
        assert_eq!(h.reviewer.as_deref(), Some("elitebook"));
        assert_eq!(h.assignment.as_deref(), Some("144ffa027a31"));
    }

    #[test]
    fn parse_review_header_verdict_only() {
        let h = parse_coord_review_header(
            "<!-- coord:review verdict=approve -->",
        )
        .expect("header present");
        assert_eq!(h.verdict.as_deref(), Some("approve"));
        assert_eq!(h.blocking, None);
        assert_eq!(h.reviewer, None);
    }

    #[test]
    fn parse_review_header_returns_none_without_verdict() {
        // A coord:review HTML comment without a verdict token is invalid.
        // Refusing to return a partial header keeps the renderer from
        // showing "verdict: None" badges.
        assert!(parse_coord_review_header(
            "<!-- coord:review reviewer=elitebook -->"
        )
        .is_none());
    }

    #[test]
    fn parse_review_header_returns_none_when_header_missing() {
        assert!(parse_coord_review_header("## Review\n\nLooks fine.").is_none());
    }

    #[test]
    fn parse_review_header_ignores_unknown_tokens() {
        // Future-token tolerance: unknown keys are skipped, never panic.
        let h = parse_coord_review_header(
            "<!-- coord:review verdict=approve cost-usd=0.34 -->",
        )
        .expect("header present");
        assert_eq!(h.verdict.as_deref(), Some("approve"));
    }

    // ── #208: format_cost_usd ────────────────────────────────────────────

    #[test]
    fn format_cost_usd_two_decimal_places() {
        assert_eq!(format_cost_usd(0.34), "$0.34");
        assert_eq!(format_cost_usd(1.50), "$1.50");
        assert_eq!(format_cost_usd(42.00), "$42.00");
    }

    #[test]
    fn format_cost_usd_sub_cent_shows_lt_one_cent() {
        // Half a cent of cost is real work — render it that way instead of
        // rounding to $0.00 which reads as "free".
        assert_eq!(format_cost_usd(0.005), "< $0.01");
        assert_eq!(format_cost_usd(0.001), "< $0.01");
    }

    #[test]
    fn format_cost_usd_zero_or_negative_renders_as_zero() {
        // Defensive: should never see negative, but don't surface a
        // misleading negative dollar amount in the UI.
        assert_eq!(format_cost_usd(0.0), "$0.00");
        assert_eq!(format_cost_usd(-1.0), "$0.00");
    }

    #[test]
    fn parse_review_header_ignores_non_integer_counts() {
        // If the count token can't be parsed (e.g. corrupt header), keep
        // going rather than rejecting the whole header — verdict is still
        // useful on its own.
        let h = parse_coord_review_header(
            "<!-- coord:review verdict=approve blocking=many -->",
        )
        .expect("header present");
        assert_eq!(h.verdict.as_deref(), Some("approve"));
        assert_eq!(h.blocking, None);
    }
}
