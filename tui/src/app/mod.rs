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
//! call and on every [`ShellApp::tick`] (quadraui drives `tick` ~60Hz so
//! refreshes proceed while the user is idle).

use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use rusqlite::Connection;

use quadraui::compose::app_shell::{AppShellEvent, AppShellLayout, PanelDefinition};
use quadraui::compose::form_controller::{FormController, FormControllerEvent};
use quadraui::compose::sidebar_system::{
    NavigationMode, SidebarEvent, SidebarSectionDef, SidebarSystem,
};
use quadraui::primitives::context_menu::{
    ContextMenu, ContextMenuHit, ContextMenuItem as QuiContextMenuItem, ContextMenuItemMeasure,
    ContextMenuLayout, ContextMenuPlacement,
};
use quadraui::primitives::form::{FieldKind, Form, FormEvent, FormField};
use quadraui::primitives::toast::{ToastCorner, ToastItem, ToastSeverity, ToastStack};

use crate::settings::{
    LogCacheTtl, ModelPref, RefreshCadence, Theme, TuiSettings, ACTION_PIPELINE_REFRESH,
};
use quadraui::accelerator::{parse_key_binding, ParsedBinding};
use quadraui::{
    Backend, Badge, Chart, ChartKind, ChatController, ChatControllerEvent, ChatRole, ChatTurn,
    Color, Decoration,
    Dialog, DialogButton, DialogHit, DialogInput, DialogLayout, DialogMeasure,
    DialogSeverity, DialogTextInput,
    Key, ListItem, ListView, Modifiers, MouseButton, NamedKey,
    PipelineHit, PipelineStage as QuiPipelineStage, PipelineView as QuiPipelineView,
    Point, Reaction, Rect, ScrollDelta, ScrollMode, SectionSize, Series, ShellApp,
    ShellConfig, ShellContext, SidebarPanel, SidebarPanelHit, StageStatus, StatusBar,
    StatusBarSegment, Scrollbar, StyledSpan, StyledText, TabBar, TabItem, TextRegion, Toolbar,
    ToolbarButton, ToolbarHoverTracker, ToolbarItemMeasure, TreeRow, UiEvent, WidgetId,
    BadgeStatus, BoardCard, BoardColumn, BoardHit, BoardLayout, BoardModel, MoveDir,
    Stage,
};
use quadraui::terminal_engine::TerminalMouseKind;

pub(crate) mod types;
pub(crate) mod format;
pub(crate) mod data;
pub(crate) mod dialogs;
pub(crate) mod sidebar;
pub(crate) mod settings_ui;
pub(crate) mod render;
pub(crate) mod chat;
pub(crate) mod terminal;
pub(crate) mod sessions;
#[allow(unused_imports)]
use self::types::*;
#[allow(unused_imports)]
use self::format::*;
#[allow(unused_imports)]
use self::data::*;
#[allow(unused_imports)]
use self::dialogs::*;
#[allow(unused_imports)]
use self::sidebar::*;
#[allow(unused_imports)]
use self::settings_ui::*;
#[allow(unused_imports)]
use self::render::*;
#[allow(unused_imports)]
use self::chat::*;
#[allow(unused_imports)]
use self::terminal::*;
#[allow(unused_imports)]
use self::sessions::*;

// ─── Auto-refresh interval ────────────────────────────────────────────────────

/// Auto-run `coord notify` every 30 seconds when assignments are running.
const NOTIFY_EVERY: Duration = Duration::from_secs(30);

/// How long a toast stays visible before auto-dismissing.
const TOAST_TTL: Duration = Duration::from_secs(4);

// ─── Shared sidebar filter ────────────────────────────────────────────────────

/// State + logic for a sidebar "FILTER" search box.
///
/// Both the Board and Pipeline panels embed a quadraui `FieldKind::TextInput`
/// form at section 0 that filters issues by a case-insensitive substring on
/// issue number/title. This struct owns the query string, the cursor byte
/// offset (always kept at a UTF-8 char boundary), and the focus flag, and
/// centralises every edit/match operation so the two panels behave
/// identically rather than duplicating the keyboard arms.
#[derive(Default, Clone)]
struct SidebarFilter {
    /// Current value in the filter input (always lowercased on match, not here).
    query: String,
    /// Cursor byte offset into `query` (kept on a char boundary).
    cursor: usize,
    /// Whether the input is accepting keyboard input.
    focused: bool,
}

impl SidebarFilter {
    /// True if an issue matches the current query (empty query → all match).
    /// Case-insensitive substring on the decimal issue number or the title.
    fn matches(&self, num: u64, title: &str) -> bool {
        if self.query.is_empty() {
            return true;
        }
        let query = self.query.to_lowercase();
        let num_str = num.to_string();
        num_str.contains(&query) || title.to_lowercase().contains(&query)
    }

    /// Insert a printable char at the cursor and advance past it.
    fn insert_char(&mut self, c: char) {
        self.query.insert(self.cursor, c);
        self.cursor += c.len_utf8();
    }

    /// Delete the char before the cursor (UTF-8 aware), if any.
    fn backspace(&mut self) {
        if self.cursor > 0 {
            // Find the start of the previous char (UTF-8 aware).
            let mut prev = self.cursor - 1;
            while prev > 0 && !self.query.is_char_boundary(prev) {
                prev -= 1;
            }
            self.query.remove(prev);
            self.cursor = prev;
        }
    }

    /// Clear the query, reset the cursor, and drop focus.
    fn clear(&mut self) {
        self.query.clear();
        self.cursor = 0;
        self.focused = false;
    }

    /// Move the cursor one Unicode scalar left.
    fn cursor_left(&mut self) {
        let chars: Vec<char> = self.query.chars().collect();
        let char_pos = chars.len().min(self.cursor);
        if char_pos > 0 {
            self.cursor = chars[..char_pos - 1].iter().collect::<String>().len();
        }
    }

    /// Move the cursor one Unicode scalar right.
    fn cursor_right(&mut self) {
        let max = self.query.len();
        if self.cursor < max {
            // Advance past the next char boundary.
            let rest = &self.query[self.cursor..];
            if let Some(ch) = rest.chars().next() {
                self.cursor += ch.len_utf8();
            }
        }
    }

    /// Replace the value (e.g. from a FormEvent) and park the cursor at the end.
    fn set_value(&mut self, value: &str) {
        self.query = value.to_string();
        self.cursor = value.len();
    }

    /// Whether the query is empty (no active filter).
    fn is_empty(&self) -> bool {
        self.query.is_empty()
    }

    /// Build the quadraui `Form` rendered in the FILTER section.
    ///
    /// `id_prefix` namespaces the widget ids so two filters on screen at
    /// once (Board vs Pipeline sidebars) don't collide. `placeholder` is the
    /// greyed hint shown when the query is empty.
    fn form(&self, id_prefix: &str, placeholder: &str) -> Form {
        Form {
            id: WidgetId::new(format!("{id_prefix}-form")),
            fields: vec![FormField {
                id: WidgetId::new(format!("{id_prefix}-input")),
                label: StyledText::plain(""),
                kind: FieldKind::TextInput {
                    value: self.query.clone(),
                    placeholder: placeholder.to_string(),
                    cursor: Some(self.cursor),
                    selection_anchor: None,
                },
                hint: StyledText::plain(""),
                disabled: false,
                validation: None,
            }],
            focused_field: if self.focused {
                Some(WidgetId::new(format!("{id_prefix}-input")))
            } else {
                None
            },
            scroll_offset: 0,
            has_focus: self.focused,
        }
    }
}

// ─── Issue fuzzy finder ───────────────────────────────────────────────────────

/// #541: State for the global Telescope-style issue fuzzy-finder overlay.
///
/// Opened with Ctrl+P from any view.  The user types to fuzzy-search across
/// all issues in `data.open_issues`; j/k (or Up/Down) navigates; Enter jumps
/// to the selected issue (Pipeline if tracked, otherwise Board); Esc closes.
#[derive(Default)]
struct IssueFinder {
    /// Current search query (as-typed, not lowercased).
    query: String,
    /// Byte offset of the text cursor inside `query` (on a char boundary).
    cursor: usize,
    /// Index of the highlighted result row within the current match list.
    selected_idx: usize,
}

impl IssueFinder {
    /// Insert a printable character at the cursor position.
    fn insert_char(&mut self, c: char) {
        self.query.insert(self.cursor, c);
        self.cursor += c.len_utf8();
        self.selected_idx = 0; // reset selection on every query change
    }

    /// Delete the character before the cursor (UTF-8 safe).
    fn backspace(&mut self) {
        if self.cursor > 0 {
            let mut prev = self.cursor - 1;
            while prev > 0 && !self.query.is_char_boundary(prev) {
                prev -= 1;
            }
            self.query.remove(prev);
            self.cursor = prev;
            self.selected_idx = 0;
        }
    }

    /// Move the highlight down by one row (wraps at the end of `max`).
    fn move_down(&mut self, max: usize) {
        if max > 0 {
            self.selected_idx = (self.selected_idx + 1).min(max - 1);
        }
    }

    /// Move the highlight up by one row (clamps at 0).
    fn move_up(&mut self) {
        self.selected_idx = self.selected_idx.saturating_sub(1);
    }
}

// ─── Fleet-wide live-sessions overlay ────────────────────────────────────────

/// #628 Scope A: state for the fleet-wide live-sessions overlay.
///
/// Shows ALL `coord-*` tmux sessions across the fleet regardless of board
/// row status — a merged/Done row says nothing about whether its session is
/// still live.  Opened/closed with `L` from any non-PTY, non-modal view.
///
/// Actions (per row): [r]eattach · [K]ill · [f]stop.
#[derive(Default)]
struct LiveSessionsOverlay {
    /// Index of the highlighted session row.
    selected_idx: usize,
}

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

/// One active SSE watch session.  Stored in `CoordApp.watch_pool` keyed by
/// `assignment_id`.  Background sessions (not currently focused) keep their
/// background thread alive and accumulate lines so switching back requires no
/// reconnect.
pub(crate) struct WatchContext {
    state: WatchState,
    sse: WatchSseState,
    /// Per-assignment inject transcript.  Persists while the context lives in
    /// the pool; cleared on eviction.
    inject_transcript: Vec<ChatTurn>,
    /// #264: parallel to `inject_transcript` — for each user turn, the
    /// `sse.lines.len()` at the moment of submit.  Used to interleave
    /// user + assistant turns chronologically in `chat_transcript_from_pool`
    /// (assistant turns at sse position N appear after any user turns whose
    /// offset ≤ N).  Without this, all user turns piled at the top and all
    /// assistant turns followed, breaking the conversational order the user
    /// expects.
    inject_sse_offsets: Vec<usize>,
    /// #315: frozen transcript turns carried over from prior workers in the
    /// same chat session.  Populated by `maybe_bind_pending_resume` when a
    /// `--resume` re-dispatch lands: it captures the prior context's full
    /// rendered chat (user + assistant turns) and stashes it here so the
    /// rebound overlay shows the prior conversation rather than starting
    /// from a blank slate.  Always rendered first by
    /// `chat_transcript_from_pool`; empty on first-dispatch contexts.
    history_turns: Vec<ChatTurn>,
    /// Wall-clock time this context was most recently focused.  Used for LRU
    /// eviction when the pool would exceed `WATCH_POOL_CAP`.
    last_focused_at: Instant,
}

/// #235 Phase 1: in-flight `coord test <work_id>` build job spawned from
/// the TUI's local machine. Re-uses the existing CLI which does git fetch +
/// checkout + `repo.build_command`, so no Rust-side git/build logic is
/// duplicated here. Job state lives in-memory only — restarting the TUI
/// drops it; the user can re-press `B` to retrigger.
/// #264: state for a refinement-chat dispatch that has been shelled but
/// whose assignment row hasn't appeared in the DB yet.  Polled each tick;
/// on bind we add the new assignment to `watch_pool`, focus it, and open
/// the inject_chat overlay.  On timeout we drop and toast.
#[derive(Clone)]
struct PendingRefinement {
    /// Coord-local repo name (matches `coordinator.yml`).
    repo: String,
    /// Issue number under refinement.
    issue_number: u64,
    /// Wall-clock dispatch instant — used to bound the wait at
    /// `REFINEMENT_BIND_TIMEOUT`.
    dispatched_at: Instant,
}

/// How long to wait for the refinement assignment row to appear before
/// giving up on the chat overlay.  `coord refine-chat` typically writes
/// the row within ~1 s; 30 s is a generous ceiling for slow agents.
const REFINEMENT_BIND_TIMEOUT: Duration = Duration::from_secs(30);

/// #314 Phase B: state carried while we wait for a `coord test-chat
/// <work_assignment_id>` dispatch to appear in the local DB so the inject-chat
/// overlay can be bound to it.  Mirrors `PendingRefinement`.
#[derive(Clone)]
struct PendingTestChat {
    /// Coord-local repo name — used in toast copy.
    repo: String,
    /// The work assignment id passed to `coord test-chat`.
    work_assignment_id: String,
    /// Issue number — used to filter `self.data.assignments`.
    issue_number: u64,
    /// Wall-clock dispatch instant — bounds the wait at
    /// `REFINEMENT_BIND_TIMEOUT`.
    dispatched_at: Instant,
}

/// #315: state carried while we wait for a re-dispatched `coord chat-continue`
/// assignment to appear in the local DB so the inject-chat overlay can rebind
/// to the new assignment (and keep the session alive across `end_turn` exits).
#[derive(Clone)]
struct PendingChatResume {
    /// The prior (completed) assignment id — used to avoid rebinding to the
    /// same row that just exited.
    old_assignment_id: String,
    /// Issue number, for filtering `self.data.assignments`.
    issue_number: u64,
    /// Wall-clock instant when `coord chat-continue` was launched.  Used
    /// for the timeout (`REFINEMENT_BIND_TIMEOUT`).
    dispatched_at: Instant,
    /// Unix-time floor for matching new assignments.  Bind only accepts
    /// rows whose `dispatched_at >= arm_unix_secs - clock-skew grace`.
    /// Without this, the bind picks ANY older refinement on the same
    /// issue that happens to pass the `id != old` filter — bug spotted
    /// in repro logs where the second submit immediately bound BACKWARDS
    /// to the original (initial) refinement assignment because the new
    /// resume row hadn't been written to the DB yet.
    arm_unix_secs: f64,
    /// The `assignment_type` of the OLD (completed) assignment — used to
    /// match the continuation to the same chat type (e.g. "refinement",
    /// "test-chat", "new-issue-chat").  Without this the bind was
    /// hardcoded to "refinement" and test-chat / new-issue-chat
    /// continuations never rebound (#361).
    old_type: Option<String>,
}

/// #264: after the refinement chat closes we issue `coord stop` first,
/// then `coord ready` once the stop returns.  CommandRunner is single-slot
/// so we can't chain them in one call without a shell wrapper; this state
/// queues the follow-up `ready` for the next tick after `stop` completes.
#[derive(Clone)]
struct PendingRefineReady {
    repo: String,
    issue_number: u64,
    /// The assignment id of the refinement worker — only used to recognise
    /// "this is the stop we were waiting on" when CommandResult lands.
    assignment_id: String,
    /// #410: when true, queue a `coord assign` dispatch immediately after
    /// `coord ready` succeeds (Send = Save + dispatch to pipeline).
    then_dispatch: bool,
}

/// #319 Phase A: synth prompt injected into the refinement chat when the
/// user presses Ctrl+N to draft refinement notes.  `{DATE}` is substituted
/// with today's date before sending so the chat's header is dated even if
/// the assistant misreads the placeholder.
const REFINEMENT_NOTES_SYNTH_PROMPT: &str = "The developer has marked this refinement complete.  Write a concise \"refinement notes\" comment summarising what we settled on:\n  - Scope decisions (what's in, what's out)\n  - File / module boundaries that came up\n  - Acceptance criteria the developer confirmed\n  - Open questions left for the implementing worker\n\nOutput the comment body in markdown.  Start with a `## Refinement notes ({DATE})` header.  Stop after the comment — the developer's TUI will post it.";

/// #319 Phase A: ceiling on how long to wait for the assistant's reply
/// before dropping back to the chat.  Generous — long refinements can
/// take a minute to settle, and the user can still cancel manually.
const REFINEMENT_NOTES_SYNTH_TIMEOUT: Duration = Duration::from_secs(180);

/// #319 Phase A: state held while we wait for the chat to reply to the
/// synth prompt.  Populated by [`CoordApp::trigger_refinement_notes_synth`]
/// and cleared once the review modal opens or the wait times out.
#[derive(Clone)]
struct PendingRefinementNotesSynth {
    /// Assignment id at trigger time.  May be superseded by a chat-continue
    /// resume — the poll re-fetches `watch_focused` to find the live id.
    aid_at_trigger: String,
    /// Issue number — used to address the `gh issue comment` shell-out.
    issue_number: u64,
    /// Coord-local repo name (only used in toasts; the slug is below).
    #[allow(dead_code)]
    repo_coord: String,
    /// GitHub `owner/name` slug for the `gh` command's `--repo` flag.
    repo_github: String,
    /// `sse.lines.len()` at trigger time — assistant lines past this index
    /// are the reply we want to capture.  Becomes meaningless if the chat
    /// rebinds (new ctx starts at 0); the poll handles that by floor=0.
    baseline_sse_lines: usize,
    /// Wall-clock arm time.  Bounds the wait at
    /// [`REFINEMENT_NOTES_SYNTH_TIMEOUT`].
    armed_at: Instant,
}

/// #319 Phase A: review-and-post modal for the proposed refinement-notes
/// comment.  When `Some` on [`CoordApp`], intercepts all keyboard input and
/// renders an overlay above the chat.  The body is editable inline:
/// Backspace pops, Enter inserts a newline, printable chars append.  Ctrl+Y
/// posts via `gh issue comment`; Esc cancels.
#[derive(Clone)]
struct RefinementNotesModal {
    /// Issue number — for the `gh` command and the modal title.
    issue_number: u64,
    /// GitHub `owner/name` slug for the `--repo` flag.
    repo_github: String,
    /// The proposed comment body.  Mutated by edits.
    body: String,
    /// True while a `gh issue comment` shell-out is in flight.  Modal
    /// stays visible to capture the result; new keypresses are ignored
    /// so a fast user can't double-post.
    posting: bool,
}

/// #319 Phase A: outcome of the background `gh issue comment` shell-out,
/// sent over the mpsc back to the main thread.
struct RefinementNotesPostResult {
    success: bool,
    /// First non-empty stderr line — surfaced in the failure toast so the
    /// user knows whether it was a missing label, auth issue, etc.
    stderr_first_line: String,
    /// Echoed so the result-handler doesn't need to read modal state.
    issue_number: u64,
}

/// #328: Y/N/Esc prompt that appears when the user presses Esc on a
/// non-empty refinement chat.  The chat overlay stays visible behind the
/// prompt; the prompt is rendered as a status-bar hint (mirrors
/// `pending_test_fail` / `pending_force_merge`).  Cleared when the user
/// picks Y (chain into notes finaliser), N (immediate finalise), or Esc
/// (cancel — chat stays open).
#[derive(Clone)]
struct PendingRefinementClosePrompt {
    /// Issue number — only used for the prompt copy.  finalise_refinement_chat
    /// and trigger_refinement_notes_synth both re-resolve via
    /// `focused_watch_state`, so we don't need to carry the aid.
    issue_number: u64,
}

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

/// #434: Durable record of a `coord pull-artifact` run.
///
/// Stored on `CoordApp.last_artifact_pulls` keyed by `work_id`.  A new
/// pull for the same work id replaces the prior entry.
#[derive(Clone)]
struct ArtifactPullResult {
    /// Exit code from `coord pull-artifact`.  0 = success.
    exit_code: i32,
    /// On success: the local destination path (`~/.coord/artifacts/<repo>/<sanitized>/`).
    /// On failure: the first meaningful stderr line, or a generic message.
    message: String,
    /// When the pull finished, used by the renderer to format an "Xs ago" hint.
    finished_at: Instant,
}

/// #532: State for the re-openable artifact-pull info dialog.
///
/// Shown after `coord pull-artifact` completes (success or failure), and
/// re-openable at any time by pressing `a` again on the same pipeline row.
#[derive(Clone, Debug, PartialEq, Eq)]
struct ArtifactPullDialog {
    /// The local destination path on success, or `None` on failure / absence.
    /// Only `Some` when a copy-to-clipboard action is meaningful.
    path: Option<String>,
    /// Human-readable body text rendered in the dialog.
    body: String,
}

/// #532: Outcome of a key press while the artifact-pull dialog is visible.
///
/// Pulled out so the key-intercept block in `handle()` and the unit tests
/// share the *same* matching code — fixing a typo in the key match (e.g.
/// `Escape` → `Back`) would now break tests, instead of leaving them passing
/// against a stale copy of the routing logic.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ArtifactDialogKeyOutcome {
    /// Copy the cached path to clipboard, push a confirmation toast, then dismiss.
    /// Only returned when the dialog carries a path (success case).
    CopyAndDismiss,
    /// Explicit dismiss key (Esc / Enter) — close the dialog with no side-effects.
    Dismiss,
    /// Anything else — swallow the key but keep the dialog visible.
    Swallow,
}

/// #532: True when the completing `pull-artifact` command result will be
/// surfaced by the artifact-pull dialog, so the generic "Command failed"
/// toast should be suppressed to avoid double-notifying the user.
///
/// The match is intentionally narrow: it returns false if the user has
/// kicked off a SECOND pull on a different row while the first is still
/// in flight (`pending_artifact_pull` then carries the newer work_id).
/// In that case the dialog handler will skip the stale result and the
/// generic toast is the ONLY signal the user has — losing it would mean
/// a silent failure.  Was the root cause of the iteration-2 regression
/// the reviewer flagged.
fn should_suppress_command_failed_toast(
    completed_label: &str,
    pending: Option<&(String, String, String)>,
) -> bool {
    if let Some((wid, _, _)) = pending {
        completed_label.contains(&format!("pull-artifact {}", wid))
    } else {
        false
    }
}

/// #532: Classify a key press while the artifact-pull dialog is open.
///
/// Pure function so tests can exhaustively drive every key path without
/// needing a Backend / ShellContext mock to call into `handle()`.
fn classify_artifact_pull_dialog_key(key: &Key, has_path: bool) -> ArtifactDialogKeyOutcome {
    match key {
        Key::Char('c') | Key::Char('C') if has_path => ArtifactDialogKeyOutcome::CopyAndDismiss,
        Key::Named(NamedKey::Escape) | Key::Named(NamedKey::Enter) => {
            ArtifactDialogKeyOutcome::Dismiss
        }
        _ => ArtifactDialogKeyOutcome::Swallow,
    }
}

/// #532: What pressing `a` on a pipeline row should do for the artifact flow.
///
/// Computed in one read-only place (`compute_a_key_artifact_action`) so the
/// production handler and the tests both route through the same decision.
#[derive(Clone, Debug, PartialEq, Eq)]
enum AKeyArtifactAction {
    /// A prior `coord pull-artifact` result is cached — re-open its dialog
    /// rather than re-running the pull command.
    ReopenDialog(ArtifactPullDialog),
    /// No cached result yet but the badge is visible — start a fresh pull
    /// against this work_id.  The producer fields are needed to seed
    /// `pending_artifact_pull` so the completion handler can match the label.
    StartPull {
        work_id: String,
        repo: String,
        sanitized: String,
    },
    /// No cached result, no badge — but we have a recorded #433 absence
    /// reason.  Open a dialog explaining why.
    ShowAbsence(ArtifactPullDialog),
}

/// #316 Phase A+C: pending board-chat dispatch.  Armed by
/// `dispatch_board_chat_new_issue()` / `dispatch_board_chat_refine()`; cleared
/// on bind or timeout (same poll + bind lifecycle as `PendingRefinement`).
#[derive(Clone)]
struct PendingBoardChat {
    repo: String,
    /// "new-issue-chat" or "refinement"
    assignment_type: String,
    dispatched_at: Instant,
}

/// #353: pending repo picker for the [Add] button on the Board panel.
/// When multiple repos exist, this shows a numeric picker (1, 2, …) for
/// the user to select which repo to open a refine-board chat for.
/// Armed when the user clicks the [Add] button and there are multiple repos.
/// Cleared when the user selects a repo (numeric key), cancels (Esc), or
/// the selection timeout expires.
#[derive(Clone)]
#[allow(dead_code)]
struct PendingRepoPicker {
    /// List of available repos (from `board_repo_names`).
    repos: Vec<String>,
    /// Currently selected index (0-based). Used for visual feedback if desired.
    selected: Option<usize>,
    /// Time the picker was opened.
    opened_at: Instant,
}

/// #486 Leg 4: pending fleet-machine picker for an interactive launch.
///
/// Armed by `launch_interactive_session_for_selected_issue` (Review/Fix only)
/// when more than one fleet machine can run the selected issue's repo, so the
/// operator can dispatch a remote review/fix over ssh+tmux from a board card
/// instead of always launching locally.  A numeric key (1, 2, …) picks the
/// machine and immediately launches; Esc cancels.  Mirrors `PendingRepoPicker`.
#[derive(Clone)]
struct PendingMachinePicker {
    /// Which interactive flavour to launch once a machine is chosen.
    mode: InteractiveLaunchMode,
    /// Candidate machines, local first, then reachable, then by name.
    machines: Vec<MachinePickEntry>,
}

/// One selectable machine in `PendingMachinePicker`.
#[derive(Clone)]
pub(crate) struct MachinePickEntry {
    /// Coordinator machine NAME (the `coord assign` positional, not the host).
    name: String,
    /// Tailscale FQDN — shown so the operator knows where the ssh lands.
    host: String,
    /// Live agent reachability (TCP/health probe) at board-build time.
    reachable: bool,
    /// True when this is the coordinator's own machine (launches on the TTY).
    is_local: bool,
}

/// #316 Phase B: state for the file-issue finaliser modal.
/// Shown when the user confirms filing a new issue drafted by a new-issue-chat.
///
/// **Current scope: preview-only.**  The modal renders the parsed title + body
/// for confirmation but does not yet accept edits — Ctrl+Y files via
/// `gh issue create`, Esc cancels.  Inline editing is tracked separately and
/// will reuse the existing notes-modal text-edit primitives when wired up.
#[derive(Clone)]
struct FileIssueModal {
    /// Parsed title from the `TITLE: …` line in the chat transcript.
    title: String,
    /// Issue body text (everything after the `---` separator).
    body: String,
    /// GitHub `owner/name` slug for `--repo`.
    repo_github: String,
    /// True while `gh issue create` is in flight.
    submitting: bool,
}

/// Result sent from the background `gh issue create` thread.
struct FileIssuePostResult {
    success: bool,
    issue_url: String,
    stderr_first_line: String,
}

// ─── Sidebar views ────────────────────────────────────────────────────────────

// ─── App data model ───────────────────────────────────────────────────────────

// ─── #207: Machine metrics (CPU + memory) ───────────────────────────────────

// ─── #336: Artifact manifest types ──────────────────────────────────────────

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
    /// #406: GitHub milestone number.  `None` for unassigned issues.
    /// Used by the Board sidebar to group issues under milestone headers.
    milestone_number: Option<i64>,
    /// #406: GitHub milestone title (e.g. "v0.5").  `None` when no milestone
    /// is assigned.  Displayed in the milestone group header.
    milestone_title: Option<String>,
}

/// #337: returns `true` for assignment types that represent real work
/// dispatched to a worker — `work`, `review`, `smoke`, `conflict-fix`,
/// and any `fix-N` variant.  Chat/scoping types (`refinement`,
/// `test-chat`, `new-issue-chat`) return `false`.
///
/// Used by `IssueGroup::lifecycle_section` so that an issue that only
/// has chat-type assignments still lands in Backlog / Refining / Refined
/// rather than being dragged into In-flight.
fn is_workable_type(ty: &str) -> bool {
    matches!(ty, "work" | "review" | "smoke" | "conflict-fix") || ty.starts_with("fix-")
}

impl IssueGroup {
    #[allow(dead_code)]
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
    /// - No real-work assignments → split into Backlog/Refining/Refined by label.
    /// - Issue cache says closed → **Completed**.
    /// - Issue is `merged` (PR closed the issue via `fixes #N`) →
    ///   **Completed**, even when the brain hasn't synced yet.
    /// - Issue has no open-cache record AND assignment is settled
    ///   (`done`/`merged`) → **Completed**.
    /// - Otherwise (with real-work assignments) → **In-flight**.
    fn lifecycle_section(&self) -> &'static str {
        // #337 / #264: chat/scoping assignment types (`refinement`,
        // `test-chat`, `new-issue-chat`) are conversational — they must
        // not drag an issue out of the Backlog/Refining/Refined buckets
        // into In-flight.  Only workable types count (see
        // `is_workable_type`).  None defaults to "work" which is workable.
        let has_real_work = self.assignments.iter().any(|a| {
            a.assignment_type
                .as_deref()
                .map(is_workable_type)
                .unwrap_or(true) // None → "work" → workable
        });
        if !has_real_work {
            // #628: a pre-work issue is just Backlog. `status:ready` gates
            // nothing (no dispatch/plan/merge path reads it), so it no longer
            // splits a separate "Refined" bucket — and `status:refining` was
            // eliminated too. The Board is Backlog → In-flight → Completed; the
            // Pipeline (coord-tracked) is the work queue.
            return "backlog";
        }
        if self.is_closed {
            return "completed";
        }
        if self.status_summary == "merged" {
            return "completed";
        }
        if !self.has_open_record && matches!(self.status_summary.as_str(), "done" | "merged") {
            return "completed";
        }
        "in-flight"
    }
}

/// #259: state of an open right-click context menu.  `None` on
/// `CoordApp.pending_context_menu` means no menu is showing.
#[derive(Clone, Debug)]
pub(crate) struct ContextMenuState {
    items: Vec<ContextMenuItem>,
    /// Anchor point — where the right-click landed.
    anchor: Point,
    /// Keyboard-selected item index in the root menu.  Maintained skipping
    /// separators and disabled items.
    selected_idx: usize,
    /// What the menu is "about" — used by `dispatch_context_menu_action`
    /// to route the action with row-specific data.
    target: ContextMenuTarget,
    /// #607: depth-first path of open submenus.  `submenu_path[d]` = the
    /// `item_idx` in the menu at depth `d` whose child submenu is currently
    /// open.  Empty ⟹ only the root menu is showing.
    submenu_path: Vec<usize>,
    /// #607: selected item index at each submenu depth.
    /// `submenu_selected[d]` is the selection inside the submenu opened by
    /// `submenu_path[d]`.
    submenu_selected: Vec<usize>,
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

/// Format a Unix timestamp as a human-readable "X ago" string relative to `now`.
///
/// Used in the Merge Queue panel (#777) to show `enqueued_at` and `last_attempt`.
/// Returns `""` for `None` timestamps.  Precision caps at minutes for < 1h,
/// hours for < 24h, days otherwise — keeps panel rows terse.
fn format_age(ts: Option<f64>, now: f64) -> String {
    let ts = match ts {
        Some(t) if t > 0.0 => t,
        _ => return String::new(),
    };
    let secs = (now - ts).max(0.0) as u64;
    if secs < 60 {
        format!("{}s ago", secs)
    } else if secs < 3600 {
        format!("{}m ago", secs / 60)
    } else if secs < 86400 {
        format!("{}h ago", secs / 3600)
    } else {
        format!("{}d ago", secs / 86400)
    }
}

/// #541: Build styled spans for `text` with per-character fuzzy-match highlights.
///
/// Characters at positions returned by `fuzzy_score(query, text)` are coloured
/// with `match_fg`; all other characters use `normal_fg`.  Returns a single
/// span in `normal_fg` when the query is empty, the text is empty, or no
/// subsequence match exists (fall-back: caller already filtered to matches).
fn styled_match_spans(
    query: &str,
    text: &str,
    normal_fg: Color,
    match_fg: Color,
) -> Vec<StyledSpan> {
    if query.is_empty() || text.is_empty() {
        return vec![StyledSpan::with_fg(text, normal_fg)];
    }
    let Some((_score, positions)) = fuzzy_score(query, text) else {
        // No match in this sub-string (possible when query matched across
        // the number+title boundary but not in title alone).  Fall back to
        // uniform colouring.
        return vec![StyledSpan::with_fg(text, normal_fg)];
    };
    if positions.is_empty() {
        return vec![StyledSpan::with_fg(text, normal_fg)];
    }
    // Build a set of matched char positions for O(1) lookup.
    let pos_set: std::collections::HashSet<usize> = positions.into_iter().collect();
    let mut spans: Vec<StyledSpan> = Vec::new();
    let mut current = String::new();
    let mut current_is_match = false;
    for (i, c) in text.chars().enumerate() {
        let is_match = pos_set.contains(&i);
        if is_match != current_is_match && !current.is_empty() {
            spans.push(StyledSpan::with_fg(
                std::mem::take(&mut current),
                if current_is_match { match_fg } else { normal_fg },
            ));
        }
        current_is_match = is_match;
        current.push(c);
    }
    if !current.is_empty() {
        spans.push(StyledSpan::with_fg(
            current,
            if current_is_match { match_fg } else { normal_fg },
        ));
    }
    spans
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

/// #558: Build a minimal single-item `ListView` with a plain text message.
/// Used for loading / empty states in the Summary tab.
fn plain_list(id: &str, msg: &str, scroll_offset: usize) -> ListView {
    let item = ListItem {
        text: StyledText {
            spans: vec![StyledSpan::with_fg(msg, Color::rgb(160, 160, 160))],
        },
        icon: None,
        detail: None,
        decoration: Decoration::Normal,
    };
    ListView {
        id: WidgetId::new(id),
        title: None,
        items: vec![item],
        selected_idx: 0,
        scroll_offset,
        has_focus: false,
        bordered: true,
        h_scroll: 0,
        max_content_width: None,
        show_v_scrollbar: false,
    }
}

/// #558: Build the `ListView` for the Pipeline Summary tab from a list of
/// `SessionSummary` entries (already sorted newest-first).
///
/// Each session renders as:
///   ● <type>  <machine>  <status/verdict (coloured)>
///      "<summary text wrapped>"
fn build_summary_list_view(summaries: Vec<SessionSummary>, scroll_offset: usize) -> ListView {
    if summaries.is_empty() {
        return plain_list(
            "pipeline-summary",
            "  No session summaries yet for this issue.",
            scroll_offset,
        );
    }

    let mut items: Vec<ListItem> = Vec::new();
    let dim = Color::rgb(120, 120, 120);
    let muted = Color::rgb(170, 170, 170);
    let machine_color = Color::rgb(130, 170, 210);

    for s in &summaries {
        // Header row: ● <type>  <machine>  <status/verdict>
        let (badge_text, badge_color) = if let Some(v) = &s.verdict {
            match v.as_str() {
                "approve" => ("approve ✓", Color::rgb(120, 200, 120)),
                "request-changes" => ("request-changes ✗", Color::rgb(220, 100, 100)),
                other => (other, Color::rgb(200, 200, 70)),
            }
        } else {
            match s.status.as_str() {
                "done" => ("done", Color::rgb(120, 120, 120)),
                "failed" => ("failed", Color::rgb(220, 70, 70)),
                "advisory" => ("advisory", Color::rgb(200, 200, 70)),
                other => (other, Color::rgb(200, 200, 70)),
            }
        };

        let type_label = session_type_label(&s.session_type);

        let header = ListItem {
            text: StyledText {
                spans: vec![
                    StyledSpan::with_fg("● ", muted),
                    StyledSpan::with_fg(format!("{:<10}", type_label), muted),
                    StyledSpan::with_fg(format!("  {:<14}", s.machine), machine_color),
                    StyledSpan::with_fg(format!("  {}", badge_text), badge_color),
                ],
            },
            icon: None,
            detail: None,
            decoration: Decoration::Normal,
        };
        items.push(header);

        // Summary text (may be multi-line; each line indented).
        if !s.summary_text.is_empty() {
            for line in s.summary_text.lines() {
                let trimmed: String = line.chars().take(200).collect();
                if trimmed.is_empty() {
                    continue;
                }
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(
                            format!("   {}", trimmed),
                            Color::rgb(200, 200, 200),
                        )],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Normal,
                });
            }
        }

        // Blank separator between sessions.
        items.push(ListItem {
            text: StyledText { spans: vec![StyledSpan::with_fg(" ", dim)] },
            icon: None,
            detail: None,
            decoration: Decoration::Normal,
        });
    }

    ListView {
        id: WidgetId::new("pipeline-summary"),
        title: None,
        items,
        selected_idx: 0,
        scroll_offset,
        has_focus: false,
        bordered: true,
        h_scroll: 0,
        max_content_width: None,
        show_v_scrollbar: false,
    }
}

/// Human-readable label for a session type string.
fn session_type_label(t: &str) -> &str {
    match t {
        "work" => "Work",
        "review" => "Review",
        "fix" => "Fix",
        "plan" => "Plan",
        "re-review" | "rereview" => "Re-review",
        "refinement" => "Refinement",
        "conflict-fix" => "Conflict-fix",
        "smoke" => "Smoke",
        other => other,
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
            '\\' => match chars.next()? {
                'n' => result.push(' '),
                't' => result.push(' '),
                'r' => {}
                c => result.push(c),
            },
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

/// Return the display-detail string for a named tool given the JSON fragment
/// of the tool's object (or enclosing event). Maps tool name → relevant field.
/// Used by both the top-level `tool_use` event handler and `extract_tool_calls`
/// so the two code paths stay in sync.
fn tool_detail(name: &str, json: &str) -> String {
    match name {
        "Bash" => json_str(json, "command").unwrap_or_default(),
        "Edit" | "Write" | "Read" | "Glob" | "NotebookEdit" => {
            json_str(json, "file_path").unwrap_or_default()
        }
        "Grep" => json_str(json, "pattern").unwrap_or_default(),
        _ => String::new(),
    }
}

/// Extract (name, detail) pairs from every `tool_use` block found in `json`.
///
/// Unlike `extract_tool_names`, this returns one entry *per occurrence* (not
/// deduplicated) and includes the per-tool detail string produced by
/// `tool_detail`.  Used to render one `→ Name: detail` line per tool call in
/// assistant events that contain only tool-use content blocks.
fn extract_tool_calls(json: &str) -> Vec<(String, String)> {
    let marker = "\"type\":\"tool_use\"";
    let mut calls: Vec<(String, String)> = Vec::new();
    let mut pos = 0;
    while let Some(found) = json[pos..].find(marker) {
        let after = pos + found + marker.len();
        // Search in the next ~400 bytes for name + detail fields.
        // Round up to the next UTF-8 char boundary (same safety measure as
        // extract_tool_names — box-drawing chars are 3 bytes each and can land
        // exactly on a fixed-size boundary).
        let mut window_end = (after + 400).min(json.len());
        while window_end < json.len() && !json.is_char_boundary(window_end) {
            window_end += 1;
        }
        let window = &json[after..window_end];
        let name = json_str(window, "name").unwrap_or_else(|| "?".to_string());
        let detail = tool_detail(&name, window);
        calls.push((name, detail));
        pos = after;
    }
    calls
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
    let (Some(v_pos), Some(b_pos)) = (v_pos, b_pos) else {
        return items;
    };

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

/// Extract the first `thinking` block's text from an assistant message.
///
/// Reasoning-only turns (`"type":"thinking"` with no `text`/`tool_use` block)
/// otherwise render as a bare "Turn N" with no visible output (#302 "empty
/// turns"). Returns an empty string if no thinking block is found.
fn extract_thinking_block(json: &str) -> String {
    let marker = "\"type\":\"thinking\"";
    if let Some(pos) = json.find(marker) {
        let after = &json[pos + marker.len()..];
        if let Some(text) = json_str(after, "thinking") {
            return text;
        }
    }
    String::new()
}

fn parse_json_event(line: &str, turn_n: &mut usize) -> Option<ListItem> {
    parse_json_event_inner(line, turn_n, None)
}

fn parse_json_event_inner(
    line: &str,
    turn_n: &mut usize,
    elapsed: Option<std::time::Duration>,
) -> Option<ListItem> {
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
            let elapsed_str = match elapsed {
                Some(d) if d.as_secs() >= 1 => format!("  +{}s", d.as_secs()),
                _ => String::new(),
            };
            // #302: don't clip the turn text at 60 chars or debug-quote it —
            // the Log tab is horizontally scrollable, so show the assistant's
            // text in full on one line (newlines collapsed to spaces so it
            // stays a single scrollable row). Show BOTH text and tools when a
            // turn has both, so a turn that thinks *and* calls a tool isn't
            // rendered as bare "Turn N" with no visible output.
            let text_line = collapse_ws(&text);
            let summary = match (!text_line.is_empty(), !tools.is_empty()) {
                (true, true) => format!(
                    "[assistant] Turn {}{}  {}  tool_use={}",
                    n,
                    elapsed_str,
                    text_line,
                    tools.join(",")
                ),
                (true, false) => {
                    format!("[assistant] Turn {}{}  {}", n, elapsed_str, text_line)
                }
                (false, true) => format!(
                    "[assistant] Turn {}{}  tool_use={}",
                    n,
                    elapsed_str,
                    tools.join(",")
                ),
                (false, false) => {
                    // No text block and no tool call — a reasoning-only turn.
                    // Surface the thinking text so the row isn't blank (#302).
                    let thinking = collapse_ws(&extract_thinking_block(line));
                    if thinking.is_empty() {
                        format!("[assistant] Turn {}{}", n, elapsed_str)
                    } else {
                        format!("[assistant] Turn {}{}  💭 {}", n, elapsed_str, thinking)
                    }
                }
            };
            Some(activity_item(&summary, Color::rgb(150, 180, 240)))
        }

        "tool_use" => {
            let name = json_str(line, "name").unwrap_or_else(|| "?".to_string());
            let detail = match name.as_str() {
                "Bash" => json_str(line, "command").unwrap_or_default(),
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

        "rate_limit_event" => Some(activity_item("[rate_limit]", Color::rgb(220, 150, 50))),

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
    matches!(name, "plan" | "work" | "review")
}

/// Build a `ListItem` for one pipeline stage (plan/work/review/smoke).
///
/// `assignment` is the best-matching assignment for the stage, or `None`
/// when the stage hasn't started yet.
fn pipeline_stage_item(name: &str, assignment: Option<&Assignment>) -> ListItem {
    let (indicator, color, detail_str) = match assignment {
        None => ("  -", Color::rgb(100, 100, 100), "pending".to_string()),
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
                StyledSpan::with_fg(format!(" {:8}", name), Color::rgb(180, 180, 200)),
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
        None => ("  -", Color::rgb(100, 100, 100), "pending".to_string()),
        Some(e) => {
            let pr_label = match e.pr_number {
                Some(n) => format!("PR #{}", n),
                None => e.state.clone(),
            };
            match e.state.as_str() {
                "merged" => ("  ✓", Color::rgb(120, 200, 120), pr_label),
                "open" | "queued" => ("  ~", Color::rgb(80, 220, 80), pr_label),
                "failed" => ("  ✗", Color::rgb(220, 70, 70), pr_label),
                "human_required" => (
                    "  !",
                    Color::rgb(220, 100, 100),
                    "needs manual rebase".to_string(),
                ),
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

// ── #487: live tmux session discovery ────────────────────────────────────────

/// Leg 2 (#517): an interactive Work/Plan/Fix session the TUI launched this
/// run, armed to auto-advance to **Test** once the board shows it finished
/// (Test precedes Review — the smoke test runs before the PR/review).  The
/// struct keeps its `AutoReview` name for continuity, but the stage it now
/// offers is Test.
///
/// The trigger is strictly **board-driven**, never terminal-scraped (ToS
/// §3.7 / #437): the embedded shell does NOT exit when the interactive
/// `claude` session ends — it returns to the prompt and
/// `finalize_interactive_exit` records the work assignment as `done` + a
/// branch.  So we watch the board for a NEW done-with-branch work assignment
/// (one whose id was not already done at arm time) and then prompt.
struct ArmedAutoReview {
    /// Coordinator-local repo name (matches `Assignment.repo`).
    coord_repo: String,
    /// GitHub repo slug (the `pipeline_issues` key half), for row selection.
    repo_slug: String,
    /// GitHub issue number.
    issue_num: u64,
    /// Work assignment ids already `done` + branch when we armed.  The prompt
    /// fires only when a work aid appears that is NOT in this set — i.e. the
    /// freshly-launched interactive work, not a pre-existing completion.
    prior_done_ids: std::collections::HashSet<String>,
}

/// Leg 2 (#517): the issue whose smoke test just passed/skipped, awaiting the
/// operator's one-key confirm to launch the human-attended review (Test
/// precedes Review).  Raised by `detect_test_verdict`.
struct PendingAutoReview {
    coord_repo: String,
    repo_slug: String,
    issue_num: u64,
}

/// Which interactive stage a [`PendingStageLaunch`] offer starts.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum StageLaunchKind {
    /// request-changes review whose findings are already in the DB → `--fix-of`.
    Fix,
    /// completed work/fix → `--smoke-of` interactive testing (Test precedes
    /// Review; on pass it then advances to review).
    Test,
}

/// A one-key offer to launch the next interactive stage for an issue.  Unlike
/// the #587 rework dialog this carries NO findings input — it's used only when
/// the next step needs no operator typing: a request-changes review whose
/// findings the agent already self-reported (kind=Fix, raised by
/// `detect_review_verdict`), or a freshly-completed work/fix advancing to the
/// smoke test (kind=Test, raised by `detect_completed_interactive_work`).
struct PendingStageLaunch {
    coord_repo: String,
    repo_slug: String,
    issue_num: u64,
    kind: StageLaunchKind,
}

/// #685: action performed when the test-mode choice is confirmed.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum TestModeChoiceAction {
    /// Dispatch a headless Work assignment (`start-skip-plan`) after setting
    /// the label.
    DispatchWork,
    /// Just update the label without dispatching (right-click "Set test mode").
    SetOnly,
}

/// #685: pending dialog asking the operator to choose the test-mode policy
/// before a headless Work session starts (or to flip it via right-click).
///
/// `[1] Pause for smoke test` (default) → `test-mode:smoke`.
/// `[2] Fully automated` → `test-mode:auto`.
///
/// On confirm the TUI queues `coord set-test-mode` then `coord assign`
/// (for DispatchWork) or only `coord set-test-mode` (for SetOnly).
#[derive(Clone)]
pub(crate) struct PendingTestModeChoice {
    coord_repo: String,
    issue_num: u64,
    /// What to do after the mode is chosen.
    action: TestModeChoiceAction,
    /// Currently set test-mode (from `all_labels`), used to pre-select the default.
    current_mode: Option<String>,
    /// `coord assign` machine name (used for DispatchWork).
    machine_name: Option<String>,
    /// `coord assign` model override (used for DispatchWork).
    model_override: Option<String>,
}

/// Leg 3 (#517): an interactive review the TUI launched this run, armed to
/// route on its verdict (board-driven, via `coord report-result`).  Fires once
/// when a NEW verdict appears (a review id not in `prior_verdicted_ids`):
/// request-changes → rework prompt; approve → smoke/merge notice.
struct ArmedVerdict {
    coord_repo: String,
    repo_slug: String,
    issue_num: u64,
    /// Review assignment ids that already carried a verdict when we armed —
    /// so only a freshly-reported verdict triggers routing.
    prior_verdicted_ids: std::collections::HashSet<String>,
}

/// Leg 3 (#517): a request-changes verdict awaiting the operator's one-key
/// confirm to launch the human-attended `--fix-of` session.  The exact review
/// id is re-resolved at confirm time (`selected_request_changes_review_aid`)
/// against the selected row, so only the issue identity is held here.
///
/// `findings` is populated as the operator types in the rework dialog (#587).
/// Written to the DB via `coord set-review-findings` on confirm, so the fix
/// worker's `_load_review_findings` DB cache hit gives it concrete feedback
/// instead of the "(No structured findings were captured)" fallback.
struct PendingRework {
    coord_repo: String,
    repo_slug: String,
    issue_num: u64,
    /// Reviewer findings typed by the operator.  Required before the fix
    /// can be dispatched — the rework dialog blocks confirm when empty.
    findings: String,
}

/// Leg 3c / A3 (#517, #581): an interactive testing session the TUI launched,
/// armed to route on its verdict (board-driven, via `coord test --passed|--fail`
/// recorded on the WORK row).  Fires once when the work row's `test_state`
/// changes to a terminal value: `failed` → fail→fix prompt; `passed`/`skipped`
/// → pass→merge prompt.
struct ArmedTestVerdict {
    coord_repo: String,
    repo_slug: String,
    issue_num: u64,
    /// The WORK assignment id under test — its `test_state` carries the verdict.
    work_aid: String,
    /// The work row's `test_state` when we armed, so only a NEW verdict fires.
    prior_test_state: Option<String>,
}

/// Leg 3c (#517, #581): a failed manual test awaiting the operator's one-key
/// confirm to launch the human-attended `--fix-of` fix on the existing branch.
/// `work_aid` is the failed WORK id (the backend's #581 test-fail fix front
/// door accepts it directly).
struct PendingTestFix {
    coord_repo: String,
    repo_slug: String,
    issue_num: u64,
}

/// Leg 3c (#517, #306): a passed test awaiting the operator's one-key confirm
/// to launch the human-attended `--merge-of` merge agent (proactive rebase +
/// conflict resolution) on the approved branch.
struct PendingMerge {
    coord_repo: String,
    repo_slug: String,
    issue_num: u64,
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
fn tui_pipeline_layout(view: &QuiPipelineView, rect: Rect) -> quadraui::PipelineViewLayout {
    let action_h = if view.stages.iter().any(|s| s.action.is_some()) {
        PIPELINE_ACTION_HEIGHT
    } else {
        0.0
    };
    view.layout(
        rect.x,
        rect.y,
        quadraui::PipelineViewMeasure::new(rect.width, rect.height, PIPELINE_ARROW_WIDTH, action_h),
    )
}

/// Status badge text + colour for the Pipeline sidebar row.
///
/// Colour mapping (semantic → `quadraui::Theme` field, overridden per palette):
/// - `"work"`   → `theme.link_fg`              (blue: active work item)
/// - `"review"` → `theme.badge_request_changes` (amber: awaiting review)
/// - `"smoke"`  → `theme.diagnostic_hint`       (violet: smoke-test gate)
/// - `"merge"`  → `theme.accent_fg`             (blue: merge stage)
/// - `"done"`   → `theme.badge_passed`          (green: completed)
/// - other      → `theme.muted_fg`              (gray: unknown/other)
fn stage_badge(stage: &str, theme: &quadraui::Theme) -> (String, Color) {
    match stage {
        "work" => ("work".into(), theme.link_fg),
        "review" => ("review".into(), theme.badge_request_changes),
        "smoke" => ("smoke".into(), theme.diagnostic_hint),
        "merge" => ("merge".into(), theme.accent_fg),
        "done" => ("done".into(), theme.badge_passed),
        other => (other.to_string(), theme.muted_fg),
    }
}


/// Compute the cache TTL (seconds) for a CI-check entry, based on the
/// current CI state and whether the PR is merge-eligible.
///
/// Returns `Some(secs)` when a cached entry older than `secs` should trigger
/// a fresh `gh pr checks` call.  Returns `None` when the entry should **not**
/// be polled at all (ineligible PR with no cached data — CI state is
/// irrelevant until the review gate clears).
///
/// Tiering:
/// * `Some(s)` where `s.running > 0` → `Some(30)` — CI still in flight;
///   needs timely updates.
/// * `Some(s)` where all checks settled (terminal) → `Some(600)` — CI result
///   won't change; a 10-minute re-check is conservative enough.
/// * `None` and `merge_eligible` → `Some(0)` — no prior fetch; eligible PR
///   needs a result immediately.
/// * `None` and `!merge_eligible` → `None` — blocked on review; skip until
///   eligibility changes.
fn ci_stale_secs(cached: Option<&CiCheckSummary>, merge_eligible: bool) -> Option<u64> {
    match cached {
        Some(s) if s.running > 0 => Some(30),
        Some(_) => Some(600),
        None if merge_eligible => Some(0),
        None => None,
    }
}

// ─── Log items cache ──────────────────────────────────────────────────────────

/// Resumable parser state for [`parse_sse_log_more`].
///
/// Stored in [`LogItemsCache`] so incremental re-parses resume from where the
/// last parse left off — only newly appended SSE lines need to be processed.
/// Defaults to the "before any lines" state via `Default`.
#[derive(Default, Clone)]
pub(crate) struct LogParseState {
    turn_n: usize,
    assistant_idx: usize,
    last_assistant_time: Option<std::time::Instant>,
    /// Last `user.timestamp` epoch (seconds) seen during the pre-pass.  Carried
    /// across calls so that `user_epochs` can be extended with only new lines.
    last_user_epoch: Option<f64>,
    /// `user_epoch_per_turn` result, extended incrementally.  One entry per
    /// `"type":"assistant"` event in order.
    user_epochs: Vec<Option<f64>>,
}

/// Cached result of parsing a log's SSE (or file) content into [`ListItem`]s.
///
/// Invalidated fully when the assignment ID or wrap width changes, or when
/// `line_count` *shrinks* (shouldn't happen, but defensive).  When `line_count`
/// grows and the assignment/width are unchanged, the cache is extended
/// incrementally by [`parse_sse_log_more`] — only the new lines are parsed.
struct LogItemsCache {
    assignment_id: String,
    /// Number of SSE lines (or file bytes for local logs) at the time of caching.
    line_count: usize,
    wrap_width: usize,
    items: Vec<ListItem>,
    /// Resumed parser state — allows O(new_lines) incremental append when the
    /// SSE stream grows.  Not meaningful for the file-based path (which does its
    /// own exact-match caching via byte length).
    parse_state: LogParseState,
}

/// Minimum wall-clock gap between successive tick-driven `Reaction::Redraw`s.
///
/// Caps streaming redraws at ≈15 fps.  User-input events (`dispatch_event`)
/// still bypass this and redraw immediately — the throttle lives only in `tick`.
const CONTENT_REDRAW_MIN: Duration = Duration::from_millis(66);

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
    // ── Board search / milestone state ──────────────────────────────────
    /// Filter state (query / cursor / focus) for the Board sidebar's FILTER box.
    board_search: SidebarFilter,
    /// #406/#410: Expanded state for each (repo, milestone_key) pair.  Default:
    /// expanded when the milestone has in-flight items, collapsed otherwise.
    /// milestone_key is the milestone number as a string, or `"no-milestone"`.
    board_milestone_expanded: std::collections::HashMap<(String, String), bool>,
    // ── Pipeline panel state ────────────────────────────────────────────
    /// SidebarSystem listing tracked issues grouped by state → (optionally) repo.
    pipeline_sidebar: SidebarSystem,
    /// Ordered list of repo keys (coord_repo or repo_slug) present in the
    /// current pipeline.  Used by `pipeline_groups_for_repo` and helpers;
    /// no longer maps 1-to-1 to sidebar section indices (that is
    /// `pipeline_state_section_names`).  Rebuilt on each
    /// `rebuild_pipeline_sidebar()`.
    pipeline_repo_names: Vec<String>,
    /// Ordered list of lifecycle state keys for the current pipeline sidebar
    /// sections.  Section index N in the sidebar maps to
    /// `pipeline_state_section_names[N - 1]` (section 0 is always FILTER).
    /// Only non-empty state sections appear.  Values are one of:
    /// `"new"` / `"refining"` / `"pending"` / `"in-progress"` / `"done"`.
    /// Rebuilt on each `rebuild_pipeline_sidebar()`.
    pipeline_state_section_names: Vec<&'static str>,
    /// Filter state (query / cursor / focus) for the Pipeline sidebar's FILTER box.
    pipeline_search: SidebarFilter,
    /// Expanded state for each (lifecycle_key, repo_key) sub-group in the
    /// Pipeline sidebar's New/Done sections.  Default: true (expanded).
    /// Persists across rebuilds so collapse survives refresh.
    ///
    /// Key semantics: `(lifecycle_key, repo_key)` — note the order is
    /// lifecycle-first now that lifecycle is the top-level grouping and repo
    /// is the sub-group.
    pipeline_lifecycle_expanded: std::collections::HashMap<(String, String), bool>,
    /// #668: Expanded state for milestone sub-headers in Pipeline New/Done
    /// sections.  Key: `(lifecycle_key, repo_key, milestone_key)`.
    /// Default: true (expanded).  Persists across rebuilds.
    pipeline_milestone_expanded: std::collections::HashMap<(String, String, String), bool>,
    /// Tracked issues for the Pipeline panel (loaded asynchronously via gh).
    pipeline_issues: Vec<PipelineIssue>,
    /// Selected issue index into `pipeline_issues`, if any.
    pipeline_sel: Option<usize>,
    /// Status message shown when a dispatch is queued/skipped due to no
    /// available machine. Cleared after a short TTL.
    pipeline_status: Option<(String, Instant)>,
    /// Active toasts rendered as a bottom-right overlay. Each entry pairs
    /// a `ToastItem` with the time it was added so the host can auto-expire
    /// them after a few seconds without the user dismissing manually.
    toasts: Vec<(ToastItem, Instant, ToastSeverity)>,
    /// Monotonic counter for toast widget IDs (must be unique per toast).
    next_toast_id: u64,
    /// Pool of concurrent SSE watch sessions keyed by `assignment_id`.
    /// Sessions remain alive (accumulating lines) even when not focused, so
    /// switching between issues requires no reconnect.  Capped at
    /// `WATCH_POOL_CAP`; the least-recently-focused entry is evicted when
    /// the pool would exceed the cap.
    watch_pool: std::collections::HashMap<String, WatchContext>,
    /// The `assignment_id` of the currently-focused watch context, or `None`
    /// when the watch overlay is closed.
    watch_focused: Option<String>,
    /// #386: True when `inject_chat` was opened from the Pipeline Log tab via
    /// the `i` keybind (which temporarily sets `watch_focused`).  On Esc /
    /// Cancelled the Cancelled arm uses this flag to clear `watch_focused`
    /// again, restoring the invariant that `watch_focused.is_none()` on the
    /// Log tab so j/k scroll falls through to the Log-tab arms (#308).
    inject_opened_from_log_tab: bool,
    /// Chat overlay for mid-flight worker guidance. `None` when closed.
    inject_chat: Option<ChatController>,
    /// Animation frame counter for the inject chat spinner.
    inject_spinner_frame: usize,
    /// Which tab is active in the Pipeline detail pane.
    pipeline_detail_tab: PipelineDetailTab,
    /// Which tab is active in the Board detail pane.
    board_detail_tab: BoardDetailTab,
    /// Scroll offset for the issue body on the Issue tab.
    pipeline_detail_scroll: usize,
    /// Horizontal scroll offset (chars) for the Log tab list (#302). Left/Right
    /// (and h/l) adjust it; the quadraui ListView rasteriser honors `h_scroll`
    /// and paints a horizontal scrollbar when content overflows the viewport.
    pipeline_log_hscroll: usize,
    /// Cache of remotely-fetched log items, keyed by assignment ID.
    ///
    /// Each entry stores `(fetched_at, items)`. Entries older than 30 s are
    /// re-fetched on the next render that needs them. `RefCell` is used so the
    /// cache can be updated from `&self` methods (render path).
    remote_log_cache:
        std::cell::RefCell<std::collections::HashMap<String, (Instant, Vec<ListItem>)>>,
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
    pending_log_fetches: std::cell::RefCell<
        std::collections::HashMap<String, std::sync::mpsc::Receiver<Result<String, String>>>,
    >,
    /// In-flight `gh issue view` fetches for Board Issue tab bodies that
    /// weren't in the local issues table (e.g. closed >7d ago and pruned).
    /// Keyed by `(repo_name, issue_number)`. The receiver yields `Ok(issue)`
    /// or `Err(error_message)`.
    pending_issue_fetches: std::cell::RefCell<
        std::collections::HashMap<
            (String, u64),
            std::sync::mpsc::Receiver<Result<FetchedIssue, String>>,
        >,
    >,
    /// In-memory cache for successfully-fetched single issues. Survives until
    /// the TUI restarts; the background thread also upserts into the DB so
    /// the next `load_data()` finds it. No TTL — `coord sync` is the source
    /// of truth for invalidation.
    fetched_issues_cache:
        std::cell::RefCell<std::collections::HashMap<(String, u64), FetchedIssue>>,
    /// #558: In-flight `gh issue view --json comments` fetches for the Pipeline
    /// Summary tab.  Keyed by `(repo_slug, issue_number)`.  The receiver yields
    /// the raw `comments` JSON array value, or an error string.
    pending_comments_fetches: std::cell::RefCell<
        std::collections::HashMap<
            (String, u64),
            std::sync::mpsc::Receiver<Result<serde_json::Value, String>>,
        >,
    >,
    /// #558: In-memory cache of parsed session-summary entries for the Pipeline
    /// Summary tab.  Keyed by `(repo_slug, issue_number)`.  Populated by
    /// `poll_pending_comments_fetches`; entries survive until the TUI restarts
    /// (no TTL — the user can switch away and back without a re-fetch).
    fetched_comments_cache:
        std::cell::RefCell<std::collections::HashMap<(String, u64), Vec<SessionSummary>>>,
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
    /// #296: inline reason input for "report & dispatch fix" (key `r` in
    /// Pipeline view when Test gate is actionable).  `Some(buf)` means we are
    /// accumulating the description; Enter records test_state=failed AND
    /// dispatches `coord fix <work_id> --guidance <buf>`.  Esc cancels.
    pending_report_fix: Option<String>,
    /// #264: refinement-chat dispatch is pending — we've shelled `coord
    /// refine-chat <repo> <issue>` and are polling the DB for the new
    /// `type="refinement"` assignment to appear so we can open the chat
    /// overlay bound to its id.  Cleared on bind or timeout.
    pending_refinement: Option<PendingRefinement>,
    /// #314 Phase B: test-chat dispatch is pending — we've shelled `coord
    /// test-chat <work_assignment_id>` and are polling the DB for the new
    /// `type="test-chat"` assignment to appear so we can open the chat
    /// overlay bound to its id.  Cleared on bind or timeout.
    pending_test_chat: Option<PendingTestChat>,
    /// #315: chat-continue dispatch is pending — the prior worker exited after
    /// `end_turn`, the user typed another message, and we've shelled
    /// `coord chat-continue <old_aid> <text>`.  We poll `self.data.assignments`
    /// each tick for the new running refinement row and rebind the open chat
    /// overlay to it when it appears.  Cleared on bind or timeout.
    pending_chat_resume: Option<PendingChatResume>,
    /// #315: channel for `spawn_inject_post` to signal worker-exit races
    /// (HTTP 409/410 from /inject/{id}).  The TX side is cloned and handed
    /// to each spawned POST thread; the RX side is drained on tick and the
    /// signals trigger transparent `coord chat-continue` fallbacks so the
    /// typed message isn't lost when the worker exited mid-submit.
    inject_fallback_tx: std::sync::mpsc::Sender<InjectFallback>,
    inject_fallback_rx: std::sync::mpsc::Receiver<InjectFallback>,
    /// #264: queued follow-up `coord ready` after `coord stop` completes
    /// at the end of a refinement chat.  Single-slot CommandRunner forces
    /// sequential dispatch; the poll handler fires this once stop's
    /// CommandResult arrives.
    pending_refine_ready: Option<PendingRefineReady>,
    /// #410: when true, the next `finalise_refinement_chat()` call should
    /// also queue a pipeline dispatch after `coord ready` completes.
    refine_then_dispatch: bool,
    /// #264: cache key for the last chat-transcript rebuild so tick()
    /// can skip the JSON-parsing pass when nothing changed.  Tuple is
    /// `(focused_assignment_id, sse.lines.len(), inject_transcript.len())`.
    /// Without this the rebuild ran at 60 fps over every accumulated
    /// stream-json line — quadratic in chat duration and the dominant CPU
    /// cost when the refinement overlay sat open for minutes.
    chat_transcript_cache_key: Option<(String, usize, usize)>,
    /// #264: timestamp of the most recent transcript change (user submit
    /// or assistant turn arrival).  Drives the chat's `set_busy()` —
    /// busy is true while activity is recent (≤2 s), false once the
    /// stream goes quiet.  Without this the spinner never animates so
    /// the user can't tell whether the worker is mid-reply or done.
    chat_last_activity: Option<Instant>,
    /// #264: spinner-redraw throttle.  When the chat is busy we need to
    /// force redraws to animate the spinner — but at 60 fps that burns
    /// CPU for no benefit (the spinner glyph only changes ~10 times a
    /// second).  Increment per tick, redraw every Nth, so spinner is
    /// visually smooth without a full 60 fps repaint while busy.
    chat_spinner_throttle: u8,
    /// #pause: set of machine names the user has paused via
    /// right-click → Pause routing.  Loaded from
    /// `~/.coord/paused_machines.json` (shared with the Python coordinator)
    /// and refreshed each periodic data load so out-of-band changes
    /// (`coord pause foo` from another terminal) propagate within a few
    /// seconds.
    paused_machines: std::collections::HashSet<String>,
    /// #245: pending `coord merge --force-merge` confirmation.  `Some(repo)`
    /// means the user pressed `m` while the "Checks failed" hint was visible
    /// and we're waiting for one-key confirmation before bypassing the CI
    /// gate.  `repo` is the coord-local repo name to scope the force-merge
    /// to (empty string ⇒ no scope, force-merge the whole queue).  Any key
    /// other than `y`/`Y` cancels; the early-intercept block consumes the
    /// keypress so the normal `y`-as-prefix paths can't fire.
    pending_force_merge: Option<String>,
    /// #780: pending "Merge all ready" confirmation from the Merge Queue panel.
    /// `Some(aids)` means the user pressed `a` and we are waiting for one-key
    /// confirmation before running `coord merge` to drain every READY entry.
    /// `aids` is the list of READY assignment_ids shown in the confirm prompt.
    /// Any key other than `y`/`Y` cancels.
    pending_merge_all_ready: Option<Vec<String>>,
    /// #259: open right-click context menu, or `None` if no menu is showing.
    /// Opened by right-click on a Board / Pipeline sidebar row; dismissed by
    /// click-outside, Escape, or item activation.
    pending_context_menu: Option<ContextMenuState>,
    /// #259 / #607: cached layout stack from the last render — one entry per
    /// open menu level (root + any open submenus).  Each entry is a
    /// `(ContextMenu, ContextMenuLayout)` pair so hit-testing can walk the
    /// stack deepest-first without rebuilding the menus.  Borrowed from the
    /// `&self` render path; populated on every frame while a menu is open and
    /// cleared when it dismisses.
    context_menu_layout: std::cell::RefCell<Vec<(ContextMenu, ContextMenuLayout)>>,
    /// Cached `DialogLayout` from the last prompt-dialog render — used for
    /// click hit-testing on dialog buttons.  Populated while any
    /// `pending_*` prompt dialog is visible; cleared when it dismisses.
    dialog_layout: std::cell::RefCell<Option<DialogLayout>>,
    /// Machines panel: name of machine awaiting restart confirmation.
    /// While `Some`, 'y'/'Y' fires `coord agent restart --machine <name>`;
    /// any other key cancels.
    pending_restart: Option<String>,
    /// Per-machine last time a `/health` HTTP fetch returned successfully.
    /// Updated from `apply_pending_data()`.  Used to show "Xs ago" in the
    /// Machines panel even across multiple load cycles.
    machine_last_contact: std::collections::HashMap<String, Instant>,
    /// Version of the local `coord` binary, fetched once at startup.
    /// Used to flag remote agents that are running an older version.
    local_coord_version: Option<String>,
    /// Cached visible-row count for the main panel, updated on scroll events
    /// and tick. Used by `watch_log_list` to compute a stick-to-bottom scroll
    /// offset that actually keeps the latest lines on screen (the previous
    /// hard-coded `items.len() - 40` cut off latest lines when the terminal
    /// viewport was under 40 rows).
    last_main_visible_rows: std::cell::Cell<usize>,
    /// Panel width in "backend units" (character columns for TUI, pixels for
    /// GTK) of the Pipeline Log tab's content rect, updated just before
    /// `pipeline_log_list` is called on each render.  Defaults to 120.  Used
    /// by `parse_log_content_readable` / `parse_sse_log_readable` to word-wrap
    /// long assistant text blocks so the Log tab is readable without horizontal
    /// scrolling (#385).
    last_log_panel_cols: std::cell::Cell<usize>,
    /// Panel width in backend units of the Issue tab's content rect, updated
    /// just before `board_issue_body_list` / `pipeline_issue_body_list` is
    /// called on each render.  Defaults to 120.  Used by `issue_body_list` to
    /// word-wrap long lines to the viewport width (#669).
    last_issue_panel_cols: std::cell::Cell<usize>,
    /// Minimum age in days for a done/failed assignment row to be eligible
    /// for the 'P' purge action.  Default 7.
    ///
    /// TODO: wire from coordinator.yml `purge_days` key (e.g. under a top-level
    /// `tui:` section) once the Python config layer supports it.
    purge_days: u32,
    /// Hover state for the sidebar action bar (above the Board / Pipeline
    /// tree).  Driven by `UiEvent::MouseMoved`; read at render time and
    /// passed to `backend.draw_sidebar_panel` so the rasteriser can tint
    /// the hovered button.  Cleared when the cursor leaves the bar.
    sidebar_action_bar_hover: ToolbarHoverTracker,
    /// Hover state for the view-level panel toolbar (above the main
    /// detail area).  Same shape and lifecycle as
    /// `sidebar_action_bar_hover`.
    panel_toolbar_hover: ToolbarHoverTracker,
    /// Hover state for the pipeline action bar — the `[ Go ⏎ ]` /
    /// `[ Retry ⏎ ]` strip drawn above the stage boxes when a
    /// dispatchable stage exists.  Separate from `panel_toolbar_hover`
    /// because the action bar is rendered directly with `draw_toolbar`
    /// (not through a `SidebarPanel`), so it needs its own tracker.
    pipeline_action_bar_hover: ToolbarHoverTracker,

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
    /// Pre-parsed keybindings from `settings.keybindings`. Rebuilt whenever
    /// settings are saved so lookups are O(n) equality checks, not string parses.
    parsed_keybindings: Vec<(String, ParsedBinding)>,
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
        std::collections::HashMap<
            (String, i64),
            std::sync::mpsc::Receiver<Result<FetchedPr, String>>,
        >,
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
    /// #290: issues for which a `coord merge` was just dispatched via the
    /// Pipeline view's Go button but the local DB hasn't yet confirmed the
    /// new merge_queue entry.  `merge_stage_status_for` returns Active for
    /// these issues so the Merge box turns blue and the Go button drops
    /// immediately — without waiting for the next DB refresh.  Cleared in
    /// `apply_pending_data` once the real merge_queue row lands.
    pipeline_inflight_merges: std::collections::HashSet<(String, u64)>,
    /// #319 Phase A: armed when the user triggers the refinement-notes
    /// finaliser (Ctrl+N in a refinement chat).  The tick poll watches the
    /// focused chat for the assistant's reply, captures it on `end_turn`,
    /// and opens [`Self::refinement_notes_modal`] for review-and-post.
    /// Cleared on modal open, timeout, or empty reply.
    pending_refinement_notes_synth: Option<PendingRefinementNotesSynth>,
    /// #319 Phase A: review-and-post modal showing the proposed
    /// refinement-notes comment.  When `Some`, intercepts all keyboard
    /// input — see [`Self::handle_refinement_notes_modal_key`].
    refinement_notes_modal: Option<RefinementNotesModal>,
    /// #319 Phase A: receiver for the background `gh issue comment` thread
    /// spawned by [`Self::post_refinement_notes`].  Drained on tick; the
    /// modal stays open on failure so the typed text isn't lost.
    refinement_notes_post_rx: Option<std::sync::mpsc::Receiver<RefinementNotesPostResult>>,
    /// #328: Y/N/Esc prompt shown when the user presses Esc on a refinement
    /// chat that has any user turns.  `None` ⇒ Esc finalises immediately,
    /// matching the empty-chat fast-exit.
    pending_refinement_close_prompt: Option<PendingRefinementClosePrompt>,
    /// #328: when true, a successful `gh issue comment` post triggers the
    /// finalise (stop + ready) chain — used to make the close-prompt Y
    /// path "draft, post, then mark ready" rather than leaving the chat
    /// open after a successful post.  Cleared on modal Esc or failure.
    finalise_after_notes_post: bool,
    /// #316 Phase A+C: pending board-chat dispatch — armed when the user
    /// clicks Refine or New Issue in the Board Chat tab and `coord
    /// refine-board` / `coord new-issue-chat` is running.  Polled each
    /// tick; on bind we open the chat overlay in the Board Chat tab.
    pending_board_chat: Option<PendingBoardChat>,
    /// #353: pending repo picker for the [Add] button on the Board panel.
    /// Armed when the user clicks [Add] and multiple repos exist. The picker
    /// intercepts numeric keys (1, 2, …) to select a repo, or Esc to cancel.
    /// Cleared when a repo is selected or the picker times out.
    pending_repo_picker: Option<PendingRepoPicker>,
    /// #486 Leg 4: pending fleet-machine picker for a remote interactive
    /// Review/Fix launch.  Intercepts numeric keys (1, 2, …) to pick the
    /// target machine, or Esc to cancel.  Cleared when a machine is chosen.
    pending_machine_picker: Option<PendingMachinePicker>,
    /// Force-quit confirmation.  Set when Esc/q is pressed while an
    /// interactive session is live in the Terminal tab — instead of silently
    /// swallowing the keypress (a dead end), we show a dialog so the operator
    /// can confirm quitting (the session keeps running in tmux) or cancel.
    pending_quit_confirm: bool,
    /// Set by the force-quit dialog's mouse button (the click path can't
    /// return `Reaction::Exit` directly); checked after mouse handling.
    quit_requested: bool,
    /// #316 Phase B: file-issue edit modal.  Shown after the user chooses
    /// to file the issue drafted in a new-issue-chat.  Intercepts all
    /// keyboard input while open.
    file_issue_modal: Option<FileIssueModal>,
    /// #316 Phase B: receiver for the background `gh issue create` thread.
    file_issue_post_rx: Option<std::sync::mpsc::Receiver<FileIssuePostResult>>,
    /// #336: Cache of artifact manifests keyed by `(repo, sanitized_branch)`.
    /// Entries have a 30-second TTL; stale entries trigger a background re-fetch.
    artifact_cache: std::collections::HashMap<(String, String), ArtifactCacheEntry>,
    /// #336: In-flight artifact manifest fetch.  `None` when idle.
    artifact_fetch_rx: Option<(
        (String, String),
        std::sync::mpsc::Receiver<ArtifactFetchOutcome>,
    )>,
    /// #336: Tracks a pending `coord pull-artifact` dispatch.
    /// Contains `(work_id, repo, sanitized_branch)` so we can show the
    /// destination path in a completion dialog.
    pending_artifact_pull: Option<(String, String, String)>,
    /// #434: Durable record of the last `coord pull-artifact` per work-id.
    /// Keyed by `work_id`.  Drives the persistent "Last pull: …" line in the
    /// Pipeline detail panel so the user who missed the initial dialog can still
    /// see the result.
    last_artifact_pulls: std::collections::HashMap<String, ArtifactPullResult>,
    /// #532: Open artifact-pull info dialog.  `Some` while the dialog is
    /// visible.  Cleared on dismiss (Esc / outside click / 'c' copy action).
    artifact_pull_dialog: Option<ArtifactPullDialog>,
    /// #399: Cache for parsed/wrapped log content items.
    ///
    /// Avoids the full O(n) re-parse of every log line on every render tick
    /// (~60 fps).  Rebuilt only when the assignment, line count, or wrap
    /// width changes.  `RefCell` so `pipeline_log_list` can update from `&self`.
    /// Extended incrementally (#787) when the SSE stream grows.
    log_items_cache: std::cell::RefCell<Option<LogItemsCache>>,
    /// #787: true while a tick source requested a redraw but the 15-fps
    /// throttle gate has not yet elapsed.  Cleared when the redraw fires.
    redraw_pending: bool,
    /// #787: when the most recent tick-driven `Reaction::Redraw` was emitted.
    last_redraw_at: Instant,

    // ── #349: Test-plan state ─────────────────────────────────────────────────
    /// Work-assignment IDs for which we have already queued a
    /// `coord test-plan` spawn (or are waiting for it to complete).
    /// Prevents duplicate spawns — once an ID is in this set the
    /// "Preparing plan…" placeholder is shown and no further spawns are issued
    /// until the plan lands (after which `assignment.test_plan` becomes `Some`
    /// and the set entry is irrelevant).  Cleared when the test stage focus
    /// moves to a different assignment so a re-focus always re-checks.
    test_plan_pending: std::collections::HashSet<String>,
    /// Assignment ID for which we last ran the staleness check.  Avoids
    /// re-running the git HEAD read on every tick while the same stage is
    /// focused; reset when the pipeline selection or focused stage changes.
    test_plan_staleness_checked_for: Option<String>,
    /// In-flight plan-step jobs keyed by (work_id, step_index).  Spawned
    /// when the user presses 1–9 (or `a` for pull steps) in the test stage.
    /// Polled each tick.
    test_step_jobs: std::collections::HashMap<(String, usize), TestStepJob>,
    /// Completed step exit codes keyed by (work_id, step_index).  Persists
    /// for the lifetime of the session so the rendered panel always shows the
    /// last outcome even after the job is drained.
    test_step_results: std::collections::HashMap<(String, usize), i32>,
    /// Captured stdout+stderr for completed test-plan steps, keyed by
    /// (work_id, step_index).  Displayed inline below each step row so the
    /// user can see *why* a step passed or failed without leaving the panel.
    test_step_output: std::collections::HashMap<(String, usize), String>,
    // ── #424: embedded terminal pane state ──────────────────────────────
    /// Live PTY-backed shell session shown when `active_view ==
    /// SidebarView::Terminal`.  Lazily spawned by [`tick`] the first
    /// time the user opens the Terminal view; persisted across view
    /// switches (switching to Board then back to Terminal does NOT
    /// respawn the shell).  Becomes `None` only on initial construction
    /// or if the initial spawn failed (error captured in
    /// `terminal_spawn_error`).
    terminal_session: Option<quadraui::terminal_engine::TerminalSession>,
    /// FOCUS PROTOCOL (#424):
    ///   - `true`  → keystrokes on the Terminal view encode to xterm
    ///     escape sequences and write to the PTY (`write_input`).  TUI
    ///     chrome view-switching (1/2/3/4/5) is INACTIVE in this mode;
    ///     press F12 to release focus.
    ///   - `false` → keystrokes drive normal TUI chrome (view switching,
    ///     hotkeys); the PTY output is still visible but read-only.
    /// Default on entering the Terminal view: `true`.
    /// Toggle key: F12 (chosen because it is rarely produced by the
    /// PTY and is a single key — no chord needed).
    terminal_focused: bool,
    /// Pending PTY resize target written by [`render_content`] (which
    /// only has `&self`) and applied by [`tick`] (`&mut self`).  Stores
    /// `(cols, rows)` derived from the live viewport rect divided by
    /// the backend's cell metrics.  `Cell` because render is `&self`.
    terminal_pending_dims: std::cell::Cell<Option<(u16, u16)>>,
    /// Error captured if `TerminalSession::spawn` failed (missing
    /// `$SHELL`, fork failure, etc.).  Displayed in the Terminal pane
    /// as a one-line placeholder so the user knows why nothing rendered.
    terminal_spawn_error: Option<String>,
    // ── #440: per-issue detail-view terminals ──────────────────────────
    /// Per-issue terminal sessions for the Pipeline detail Terminal tab.
    /// Keyed by `(repo_slug, issue_number)` (#455) so that same-numbered
    /// issues in different repos (e.g. quadraui #336 vs coord #336) each
    /// get their own session.  Lazily spawned the first time the user
    /// opens the Terminal tab for a given issue; kept running while the
    /// issue stays in `pipeline_issues`; dropped when the issue leaves
    /// the pipeline (on the next load that no longer contains it).
    detail_terminal_sessions: std::collections::HashMap<(String, u64), quadraui::terminal_engine::TerminalSession>,
    /// Spawn errors for per-issue terminals (shown as a one-line
    /// placeholder in the Terminal tab).  Keyed by `(repo_slug, issue_number)`.
    detail_terminal_spawn_errors: std::collections::HashMap<(String, u64), String>,
    /// Focus state for the Pipeline detail Terminal tab.  `true` when
    /// keypresses route to the selected issue's PTY.  F12 toggles.
    /// Independent of `terminal_focused` (the standalone pane's flag).
    detail_terminal_focused: bool,
    /// #605: `Ctrl-W` pane-leader latch — `true` after the user presses
    /// `Ctrl-W`, until the next key resolves the chord (`h`/`Left` → side
    /// panel, `l`/`Right` → content/PTY, `Ctrl-W` → literal to the PTY).
    /// A keyboard-only slice of the #578 focus model (no mouse focus).
    ctrl_w_pending: bool,
    /// Pending `(cols, rows)` for per-issue terminal resize, stashed by
    /// `render_detail_terminal_tab` (`&self`) and applied by `tick`
    /// (`&mut self`).
    detail_terminal_pending_dims: std::cell::Cell<Option<(u16, u16)>>,
    /// #454: bitmask of mouse buttons whose `Press` was forwarded to an
    /// embedded PTY but whose matching `Release` has not yet been
    /// forwarded.  Used so that a release fires even when the cursor
    /// has been dragged outside the terminal content area — without it,
    /// terminal apps (vim visual mode, tmux mouse drag, less) stay
    /// stuck in "button held" state until the next click.
    /// Bits: `1` = Left, `2` = Middle, `4` = Right.
    pty_pressed_buttons: u8,
    /// #464: `true` while a host-side text-selection drag is in progress in
    /// the active terminal pane. Set when mouse-reporting is OFF (plain
    /// shell) or Shift is held (force-override). Cleared on the matching
    /// mouse-release. `pty_pressed_buttons` is NOT set for these drags.
    terminal_host_sel_dragging: bool,

    // ── #207: Machine metrics sparklines ─────────────────────────────────
    /// Rolling ring-buffer of CPU + memory samples per machine, keyed by
    /// machine name.  Capped at [`METRICS_HISTORY`] entries (60 × 5 s =
    /// 5 minutes).  Populated by background `/metrics` polls.
    machine_metrics: std::collections::HashMap<String, std::collections::VecDeque<MetricSample>>,
    /// In-flight `/metrics` fetches — one entry per machine per poll cycle.
    /// Drained each tick; completed entries update `machine_metrics`.
    pending_metrics: Vec<PendingMetrics>,
    /// When the last `/metrics` poll round was kicked.  The next round fires
    /// after [`METRICS_CADENCE`] has elapsed and the Machines panel is visible.
    metrics_last_polled: Instant,

    // ── #487: live tmux session discovery ──────────────────────────────────
    /// Running `coord-*` tmux sessions discovered at startup via
    /// `coord sessions --json`.  Each entry represents an interactive
    /// session that survived a previous TUI run (or is still active in
    /// the background).  Displayed as a startup toast and used by
    /// `launch_interactive_session_for_selected_issue` to offer reattach
    /// instead of starting a fresh session when one already exists.
    live_tmux_sessions: Vec<LiveTmuxSession>,
    /// #486 Leg 4: background fetch of local + REMOTE sessions (ssh-probes the
    /// fleet).  When it lands, it REPLACES `live_tmux_sessions` (a superset of
    /// the local-only startup snapshot) so reattach can target remote sessions.
    pending_remote_sessions:
        Option<std::sync::mpsc::Receiver<Vec<LiveTmuxSession>>>,

    // ── #603: exact fix-briefing preview for the fail→fix / rework dialog ───
    /// The resolved fix briefing text (context block + findings/test story),
    /// shown in the confirm dialog so the operator sees what the fix worker
    /// gets BEFORE launching.  `Some("Resolving…")` while the async
    /// `coord fix-briefing` shell-out is in flight; cleared when the dialog is
    /// dismissed.
    fix_briefing_preview: Option<String>,
    /// In-flight `coord fix-briefing <aid>` fetch feeding `fix_briefing_preview`.
    fix_briefing_rx: Option<std::sync::mpsc::Receiver<String>>,

    // ── Leg 2 (#517): auto-advance Work → Review ───────────────────────────
    /// Interactive Work/Plan sessions launched this run, keyed by
    /// `(repo_slug, issue_number)`, armed to prompt for a review once the
    /// board shows the work finished.  Entry removed when the prompt fires
    /// or a Review is launched for the issue.  See [`ArmedAutoReview`].
    armed_for_auto_review: std::collections::HashMap<(String, u64), ArmedAutoReview>,
    /// When `Some`, an inline confirm prompt is up asking whether to launch
    /// the interactive review for a just-finished work session.  Enter
    /// confirms; Esc/n dismisses.  See [`PendingAutoReview`].
    pending_auto_review: Option<PendingAutoReview>,
    /// Post-review one-key stage offer (Fix / Test) — see [`PendingStageLaunch`].
    pending_stage_launch: Option<PendingStageLaunch>,
    /// #685: pending test-mode choice dialog.  Armed when the operator picks
    /// "Start (automated) > Work/Plan" from the Pipeline context menu or the
    /// "Set test mode" right-click item; cleared on confirm or Esc.
    pending_test_mode_choice: Option<PendingTestModeChoice>,
    /// #685: set of work assignment IDs for which the TUI already raised the
    /// interactive-smoke offer (via `detect_headless_smoke_work_done`).  Prevents
    /// the offer from re-firing on every tick after the user dismisses it.
    offered_smoke_for_headless_work: std::collections::HashSet<String>,

    // ── Leg 3 (#517): verdict-driven routing ───────────────────────────────
    /// Interactive reviews launched this run, keyed by `(repo_slug,
    /// issue_number)`, armed to route on their reported verdict.  Entry removed
    /// when the verdict is routed.  See [`ArmedVerdict`].
    armed_for_verdict: std::collections::HashMap<(String, u64), ArmedVerdict>,
    /// When `Some`, an inline confirm prompt is up asking whether to launch the
    /// interactive `--fix-of` session for a request-changes verdict.  Enter
    /// confirms; Esc dismisses.  See [`PendingRework`].
    pending_rework: Option<PendingRework>,
    /// #587: one-shot flag set by `confirm_rework` to suppress the secondary
    /// "no findings captured" gate inside `launch_interactive_session_for_selected_issue`
    /// while it routes through Fix mode to launch the session.  Cleared on
    /// the same tick, so it never survives a full user-initiated Fix request.
    rework_bypass: bool,

    // ── Leg 3c / A3 (#517, #581): test-verdict routing ─────────────────────
    /// Interactive testing sessions launched this run, keyed by `(repo_slug,
    /// issue_number)`, armed to route on the WORK row's recorded test verdict.
    /// Entry removed when the verdict is routed.  See [`ArmedTestVerdict`].
    armed_for_test_verdict: std::collections::HashMap<(String, u64), ArmedTestVerdict>,
    /// When `Some`, an inline confirm prompt is up asking whether to launch the
    /// interactive `--fix-of` fix for a FAILED test.  See [`PendingTestFix`].
    pending_test_fix: Option<PendingTestFix>,
    /// When `Some`, an inline confirm prompt is up asking whether to launch the
    /// interactive `--merge-of` merge agent after a PASSED test.  See
    /// [`PendingMerge`].
    pending_merge: Option<PendingMerge>,

    // ── #638: Kanban view ────────────────────────────────────────────────────
    kanban_model: BoardModel,
    kanban_layout: std::cell::RefCell<Option<BoardLayout>>,

    // ── #737: Merge Queue panel ──────────────────────────────────────────────
    /// Selected entry index in the Merge Queue panel list (0-based).  Clamped
    /// to `data.merge_queue.len().saturating_sub(1)` on navigation so it never
    /// goes out of bounds even after a data refresh.
    merge_queue_sel: usize,
    /// Scroll offset (first visible row index) for the Merge Queue panel list.
    /// Updated by `fix_merge_queue_scroll` after every j/k/Home/End navigation
    /// and passed as `scroll_offset` to `draw_list` so the selected item stays
    /// in the visible window.  See quadraui `primitives/list.rs`: "The app
    /// updates `selected_idx` and `scroll_offset` for the next frame."
    merge_queue_scroll: usize,

    // ── #541: global Telescope-style issue fuzzy finder ──────────────────────
    /// Active state of the issue fuzzy-finder overlay.  `None` when the
    /// overlay is closed.  Opened with Ctrl+P from any non-PTY view; closed
    /// with Esc or Enter (Enter also navigates to the selected issue).
    issue_finder: Option<IssueFinder>,

    // ── #628 Scope A: fleet-wide live-sessions overlay ────────────────────────
    /// Active state of the fleet-wide live-sessions overlay.  `None` when
    /// closed.  Opened/closed with `L` from any non-PTY, non-modal view.
    live_sessions_overlay: Option<LiveSessionsOverlay>,

    // ── #217 Theming ─────────────────────────────────────────────────────────
    /// Resolved colour palette, derived from `settings.theme` (and an optional
    /// `~/.coord/theme.toml` override file) at startup and after each settings
    /// change.  Passed into theme-sensitive rendering helpers such as
    /// `Assignment::status_color_themed` and `stage_badge`.
    active_theme: quadraui::Theme,

    // ── #728: Done-section time window ───────────────────────────────────────
    /// Controls how far back the windowed Done list reaches.  Cycled by the
    /// `→` key while the Done section is focused.  Resets to `H2` on restart.
    done_window: DoneWindow,

    // ── #816: PTY-panic modal ─────────────────────────────────────────────────
    /// When `Some`, a dismissible modal dialog is shown explaining that a vt100
    /// parser panic evicted an active terminal session.  The string is the raw
    /// panic message captured by `catch_unwind` and converted via
    /// [`vt100_panic_to_string`].  Set by [`report_terminal_panic`]; cleared
    /// when the operator dismisses the dialog (Esc / Enter / outside-click).
    pty_panic_dialog: Option<String>,
}

/// #728: Time-window for the Done section in the Pipeline sidebar.
///
/// Cycled forward (H2 → H24 → D7 → All) by the `→` key while the Done
/// section is focused.  Resets to `H2` on TUI restart (in-memory only).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DoneWindow {
    /// Last 2 hours (default).
    H2,
    /// Last 24 hours.
    H24,
    /// Last 7 days.
    D7,
    /// All done issues regardless of age.
    All,
}

impl DoneWindow {
    /// The maximum age in seconds for this window, or `None` for "All".
    fn secs(self) -> Option<f64> {
        match self {
            DoneWindow::H2 => Some(2.0 * 3600.0),
            DoneWindow::H24 => Some(24.0 * 3600.0),
            DoneWindow::D7 => Some(7.0 * 86_400.0),
            DoneWindow::All => None,
        }
    }

    /// Cycle to the next wider window.
    fn next(self) -> Self {
        match self {
            DoneWindow::H2 => DoneWindow::H24,
            DoneWindow::H24 => DoneWindow::D7,
            DoneWindow::D7 => DoneWindow::All,
            DoneWindow::All => DoneWindow::H2,
        }
    }

    /// Short label for use in the section header.
    fn label(self) -> &'static str {
        match self {
            DoneWindow::H2 => "last 2h",
            DoneWindow::H24 => "last 24h",
            DoneWindow::D7 => "last 7d",
            DoneWindow::All => "all",
        }
    }
}

impl Default for CoordApp {
    fn default() -> Self {
        Self::new()
    }
}

/// Parse all keybinding strings from a `TuiSettings` into `ParsedBinding`s.
/// Unrecognised or unparseable entries are silently skipped.
fn parse_keybindings(settings: &TuiSettings) -> Vec<(String, ParsedBinding)> {
    settings
        .keybindings
        .iter()
        .filter_map(|(action, key_str)| {
            if key_str.is_empty() {
                return None;
            }
            let parsed = parse_key_binding(key_str)?;
            Some((action.clone(), parsed))
        })
        .collect()
}

/// Normalise a `Key` to the string format used by `ParsedBinding::key`:
/// single chars are lowercased; named keys use their display name.
fn key_to_binding_str(key: &Key) -> String {
    use quadraui::NamedKey;
    match key {
        Key::Char(c) => c.to_ascii_lowercase().to_string(),
        Key::Named(n) => match n {
            NamedKey::Escape => "Escape".to_string(),
            NamedKey::Enter => "Enter".to_string(),
            NamedKey::Tab => "Tab".to_string(),
            NamedKey::Backspace => "Backspace".to_string(),
            NamedKey::Delete => "Delete".to_string(),
            NamedKey::Home => "Home".to_string(),
            NamedKey::End => "End".to_string(),
            NamedKey::PageUp => "PageUp".to_string(),
            NamedKey::PageDown => "PageDown".to_string(),
            NamedKey::Up => "Up".to_string(),
            NamedKey::Down => "Down".to_string(),
            NamedKey::Left => "Left".to_string(),
            NamedKey::Right => "Right".to_string(),
            NamedKey::F(n) => format!("F{n}"),
            _ => String::new(),
        },
    }
}

/// #638: Derive per-stage badge statuses for a Kanban card from its IssueGroup.
///
/// Returns a compact badge row: Plan → Work → Test → Review → Merge.
/// Status is inferred from the group's status_summary and assignments.
fn kanban_stage_badges(g: &IssueGroup) -> Vec<(Stage, BadgeStatus)> {
    // Determine the dominant assignment status.
    let has_running = g.assignments.iter().any(|a| a.status == "running");
    let has_failed = g.assignments.iter().any(|a| a.status == "failed");
    let status = g.status_summary.as_str();

    // Work stage status.
    let work_status = if has_running {
        BadgeStatus::Running
    } else if has_failed {
        BadgeStatus::Blocked
    } else if matches!(status, "done" | "merged") {
        BadgeStatus::Passed
    } else {
        BadgeStatus::Pending
    };

    // Review stage: check review_verdict on any assignment.
    let review_status = {
        let verdict = g.assignments.iter()
            .filter_map(|a| a.review_verdict.as_deref())
            .last();
        match verdict {
            Some("approve") => BadgeStatus::Passed,
            Some("request-changes") => BadgeStatus::RequestChanges,
            Some(_) => BadgeStatus::Running,
            None => BadgeStatus::Pending,
        }
    };

    // Merge stage: passed when status is "merged".
    let merge_status = if status == "merged" {
        BadgeStatus::Passed
    } else {
        BadgeStatus::Pending
    };

    vec![
        (Stage::Work, work_status),
        (Stage::Review, review_status),
        (Stage::Merge, merge_status),
    ]
}

impl CoordApp {
    /// Create a new app.
    /// Check whether the given key+modifiers match a named action in the user's
    /// keybindings table.  Returns the action name when matched.
    fn action_for_key<'a>(&'a self, key: &Key, modifiers: &quadraui::Modifiers) -> Option<&'a str> {
        let key_str = key_to_binding_str(key);
        if key_str.is_empty() {
            return None;
        }
        self.parsed_keybindings
            .iter()
            .find(|(_, binding)| binding.key == key_str && binding.modifiers == *modifiers)
            .map(|(action, _)| action.as_str())
    }

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
        let (inject_fallback_tx, inject_fallback_rx) = std::sync::mpsc::channel();
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
            pending_data: Some(start_data_load()),
            fetch_error: None,
            pending_log_fetches: std::cell::RefCell::new(std::collections::HashMap::new()),
            pending_issue_fetches: std::cell::RefCell::new(std::collections::HashMap::new()),
            fetched_issues_cache: std::cell::RefCell::new(std::collections::HashMap::new()),
            pending_comments_fetches: std::cell::RefCell::new(std::collections::HashMap::new()),
            fetched_comments_cache: std::cell::RefCell::new(std::collections::HashMap::new()),
            pending_purge: None,
            pending_test_fail: None,
            pending_report_fix: None,
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
            local_coord_version: fetch_local_coord_version(),
            last_main_visible_rows: std::cell::Cell::new(40),
            last_log_panel_cols: std::cell::Cell::new(120),
            last_issue_panel_cols: std::cell::Cell::new(120),
            purge_days: 7,
            sidebar_action_bar_hover: ToolbarHoverTracker::new(),
            panel_toolbar_hover: ToolbarHoverTracker::new(),
            pipeline_action_bar_hover: ToolbarHoverTracker::new(),
            pipeline_focused_stage: None,
            pipeline_stage_content_scroll: 0,
            settings: TuiSettings::load(),
            parsed_keybindings: parse_keybindings(&TuiSettings::load()),
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
            pending_repo_picker: None,
            pending_machine_picker: None,
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
            // #424: terminal pane is lazily spawned the first time the
            // user opens the Terminal view (see `tick`).  Start with no
            // session, focus enabled-by-default once the view is opened.
            terminal_session: None,
            terminal_focused: false,
            terminal_pending_dims: std::cell::Cell::new(None),
            terminal_spawn_error: None,
            // #440: per-issue detail-view terminals.
            detail_terminal_sessions: std::collections::HashMap::new(),
            detail_terminal_spawn_errors: std::collections::HashMap::new(),
            detail_terminal_focused: false,
            ctrl_w_pending: false,
            detail_terminal_pending_dims: std::cell::Cell::new(None),
            // #454: tracks Press → Release for PTY mouse-reporting drags.
            pty_pressed_buttons: 0,
            // #464: host-side terminal selection drag state.
            terminal_host_sel_dragging: false,
            // #207: machine metrics sparklines.
            machine_metrics: std::collections::HashMap::new(),
            pending_metrics: Vec::new(),
            metrics_last_polled: Instant::now(),
            // #487: live tmux session discovery.
            live_tmux_sessions: fetch_live_tmux_sessions(),
            pending_remote_sessions: Some(spawn_remote_tmux_sessions_fetch(
                crate::commands::find_config(),
            )),
            // #603: fix-briefing preview (lazily populated when a dialog raises).
            fix_briefing_preview: None,
            fix_briefing_rx: None,
            // Leg 2 (#517): auto-advance Work → Review.
            armed_for_auto_review: std::collections::HashMap::new(),
            pending_auto_review: None,
            pending_stage_launch: None,
            // #685: per-issue test-mode policy choice dialog.
            pending_test_mode_choice: None,
            offered_smoke_for_headless_work: std::collections::HashSet::new(),
            // Leg 3 (#517): verdict-driven routing.
            armed_for_verdict: std::collections::HashMap::new(),
            pending_rework: None,
            rework_bypass: false,
            // #541: global issue fuzzy finder — closed by default.
            issue_finder: None,
            // #628 Scope A: fleet-wide live-sessions overlay — closed by default.
            live_sessions_overlay: None,
            // Leg 3c / A3 (#517, #581): test-verdict routing.
            armed_for_test_verdict: std::collections::HashMap::new(),
            pending_test_fix: None,
            pending_merge: None,
            // #638: Kanban view — empty until rebuild_board_sidebar populates it.
            kanban_model: BoardModel {
                id: WidgetId::new("kanban:coord"),
                columns: Vec::new(),
                selected_card_id: None,
                col_scroll_offset: 0,
            },
            kanban_layout: std::cell::RefCell::new(None),
            // #737: Merge Queue panel — selection and scroll start at the top.
            merge_queue_sel: 0,
            merge_queue_scroll: 0,
            // #217: resolved theme palette — computed from settings + optional
            // ~/.coord/theme.toml override file.
            active_theme: {
                let s = TuiSettings::load();
                crate::settings::TuiSettings::load_custom_theme_file()
                    .unwrap_or_else(|| s.theme.to_quadraui_theme())
            },
            // #728: Done section shows last 2h by default; cycled with `→`.
            done_window: DoneWindow::H2,
            // #816: no pending PTY-panic dialog on startup.
            pty_panic_dialog: None,
        };
        // #584: a thin client pulls config from the daemon (no local
        // coordinator.yml) so the status bar doesn't warn and subcommands have
        // a config path.  Best-effort: leave config_path None if the fetch fails.
        if is_remote_board_service() {
            if let Some(cfg) = fetch_remote_config_to_cache() {
                app.command_runner.config_path = Some(cfg);
            }
        }
        app.rebuild_board_sidebar();
        app.rebuild_pipeline_sidebar(None);
        // Sync issues from GitHub on startup so the board backlog is fresh.
        app.kick_issue_sync();
        // #487: notify the operator about any live sessions from a previous run.
        if !app.live_tmux_sessions.is_empty() {
            let n = app.live_tmux_sessions.len();
            let ids: Vec<String> = app
                .live_tmux_sessions
                .iter()
                .map(|s| {
                    if let (Some(repo), Some(num)) = (&s.repo_name, s.issue_number) {
                        format!("{} #{}", repo, num)
                    } else {
                        s.assignment_id.clone()
                    }
                })
                .collect();
            let summary = ids.join(", ");
            let title = format!(
                "{} running interactive session{}",
                n,
                if n == 1 { "" } else { "s" }
            );
            let body = format!(
                "{} — open Terminal tab to reattach, or: coord reattach <id>",
                summary
            );
            app.push_toast(&title, &body, ToastSeverity::Info);
        }
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
                // #424: embedded terminal pane (PTY-backed shell via
                // quadraui::terminal_engine::TerminalSession).
                PanelDefinition {
                    id: WidgetId::new("panel:terminal"),
                    // >_ for a shell prompt glyph.
                    icon: ">_".into(),
                    tooltip: "Terminal".into(),
                    title: "TERMINAL".into(),
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
        // #559: re-arm remote session discovery so the Live/Idle split stays
        // current for the entire TUI run.  The initial arm is one-shot
        // (startup only); without this, any session started after startup
        // stays in Idle forever until the TUI is restarted.
        if self.pending_remote_sessions.is_none() {
            self.pending_remote_sessions = Some(spawn_remote_tmux_sessions_fetch(
                crate::commands::find_config(),
            ));
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
        let mut items: Vec<ToastItem> = self.toasts.iter().map(|(it, _, _)| it.clone()).collect();
        if let Some((msg, when)) = &self.pipeline_status {
            if when.elapsed() < TOAST_TTL {
                items.push(ToastItem {
                    id: WidgetId::new(format!("pipeline-status-{}", when.elapsed().as_millis())),
                    title: "Pipeline".to_string(),
                    body: msg.clone(),
                    severity: if msg.contains("no reachable")
                        || msg.contains("no failed")
                        || msg.contains("not found")
                    {
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
    ///
    /// If the assignment already has a live context in `watch_pool` (because
    /// the user switched away from it earlier), we simply re-focus it without
    /// disturbing the background stream.  Otherwise we open a new SSE
    /// connection and insert it into the pool (evicting the LRU entry if the
    /// pool has reached `WATCH_POOL_CAP`).
    fn open_watch_for_selected_issue(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
        let local_repo = issue.coord_repo.as_deref();

        let candidates: Vec<_> = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == r,
                None => true,
            })
            .collect();

        // Prefer running → any non-done → most-recent done (read-only log review).
        let pick = candidates
            .iter()
            .copied()
            .find(|a| a.status == "running")
            .or_else(|| candidates.iter().copied().find(|a| a.status != "done"))
            .or_else(|| {
                candidates.iter().copied().max_by(|a, b| {
                    a.dispatched_at
                        .partial_cmp(&b.dispatched_at)
                        .unwrap_or(std::cmp::Ordering::Equal)
                })
            });

        match pick {
            Some(a) => {
                let aid = a.id.clone();
                // Re-focus an existing stream without touching it.
                if self.watch_pool.contains_key(&aid) {
                    if let Some(ctx) = self.watch_pool.get_mut(&aid) {
                        ctx.last_focused_at = Instant::now();
                    }
                    self.watch_focused = Some(aid);
                    return true;
                }
                // New stream — build WatchState and open SSE.
                let state = WatchState {
                    assignment_id: a.id.clone(),
                    machine: a.machine.clone(),
                    repo: a.repo.clone(),
                    issue_number: a.issue_number,
                    assignment_type: a
                        .assignment_type
                        .clone()
                        .unwrap_or_else(|| "work".to_string()),
                    scroll: usize::MAX,
                };
                let sse = if let Some(m) = self.data.machines.iter().find(|m| m.name == a.machine) {
                    if !m.host.is_empty() {
                        let rx = spawn_sse_watch(&m.host, &a.id, 0);
                        WatchSseState {
                            rx,
                            lines: Vec::new(),
                            last_event_id: 0,
                            fail_count: 0,
                            first_fail_at: None,
                            done: false,
                            host: m.host.clone(),
                            pending_tail: String::new(),
                            line_times: Vec::new(),
                            current_turn: 0,
                        }
                    } else {
                        make_local_sse_state(&a.id)
                    }
                } else {
                    make_local_sse_state(&a.id)
                };
                // Evict the LRU entry if the pool is at capacity.
                if self.watch_pool.len() >= WATCH_POOL_CAP {
                    let lru_id = self
                        .watch_pool
                        .iter()
                        .min_by_key(|(_, ctx)| ctx.last_focused_at)
                        .map(|(id, _)| id.clone());
                    if let Some(id) = lru_id {
                        self.watch_pool.remove(&id);
                    }
                }
                self.watch_pool.insert(
                    aid.clone(),
                    WatchContext {
                        state,
                        sse,
                        inject_transcript: Vec::new(),
                        inject_sse_offsets: Vec::new(),
                        history_turns: Vec::new(),
                        last_focused_at: Instant::now(),
                    },
                );
                self.watch_focused = Some(aid);
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

    /// Close the watch overlay (clear focus).  The background SSE stream for
    /// the previously-focused assignment is kept alive in `watch_pool` so the
    /// user can switch back without a reconnect.
    fn close_watch(&mut self) {
        self.watch_focused = None;
        self.inject_chat = None;
        // #386: if the Log-tab steer overlay was open, clear its flag too.
        self.inject_opened_from_log_tab = false;
    }

    /// True when the Pipeline view's Log tab is the active scroller.  On this
    /// tab the visible content is `pipeline_log_list` (scrolled via
    /// `pipeline_detail_scroll`); used by the periodic `tick` to re-attach
    /// SSE when the running assignment under the cursor changes.
    fn on_pipeline_log_tab(&self) -> bool {
        self.active_view == SidebarView::Pipeline
            && self.pipeline_detail_tab == PipelineDetailTab::Log
    }

    /// Called whenever the Log tab becomes active (and on each tick while it's
    /// active).  Ensures `watch_pool` holds an SSE stream for the assignment
    /// `pipeline_log_list` would currently render — without setting
    /// `watch_focused`, so the watch-overlay j/k arms don't shadow the Log-tab
    /// scroll arms (#308).
    ///
    /// The pool check is keyed on the *picked* assignment id, not just the
    /// issue number.  Without that, auto_loop transitions
    /// (work→review→work→review on the same issue) leave a stale pool entry
    /// for the old assignment and the new running one would fall back to
    /// HTTP polling — reintroducing the "Loading log…" flicker.
    fn ensure_log_tab_sse(&mut self) {
        // Pick the same assignment pipeline_log_list does (running → most
        // recent by dispatched_at) so the pool entry tracks the visible row.
        let pick_id = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))
            .and_then(|issue| {
                let local_repo = issue.coord_repo.as_deref();
                self.data
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
                            .max_by(|a, b| {
                                a.dispatched_at
                                    .partial_cmp(&b.dispatched_at)
                                    .unwrap_or(std::cmp::Ordering::Equal)
                            })
                    })
                    .map(|a| a.id.clone())
            });
        let Some(target) = pick_id else {
            return;
        };
        // Pool already has the picked assignment? Leave it (live or done).
        if self.watch_pool.contains_key(&target) {
            return;
        }
        // No entry for the picked assignment — open one without focusing.
        self.open_sse_in_pool_for_selected_issue();
    }

    /// Open (or reuse) an SSE stream in `watch_pool` for the selected issue
    /// WITHOUT setting `watch_focused`.  Used by the Log tab and any other
    /// caller that wants background accumulation but not the watch overlay.
    fn open_sse_in_pool_for_selected_issue(&mut self) {
        let Some(idx) = self.pipeline_sel else {
            return;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return;
        };
        let local_repo = issue.coord_repo.as_deref();

        let candidates: Vec<_> = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == r,
                None => true,
            })
            .collect();

        let pick = candidates
            .iter()
            .copied()
            .find(|a| a.status == "running")
            .or_else(|| candidates.iter().copied().find(|a| a.status != "done"))
            .or_else(|| {
                candidates.iter().copied().max_by(|a, b| {
                    a.dispatched_at
                        .partial_cmp(&b.dispatched_at)
                        .unwrap_or(std::cmp::Ordering::Equal)
                })
            });

        let Some(a) = pick else {
            return;
        };
        let aid = a.id.clone();

        // Already in the pool — nothing to do.
        if self.watch_pool.contains_key(&aid) {
            return;
        }
        let state = WatchState {
            assignment_id: a.id.clone(),
            machine: a.machine.clone(),
            repo: a.repo.clone(),
            issue_number: a.issue_number,
            assignment_type: a
                .assignment_type
                .clone()
                .unwrap_or_else(|| "work".to_string()),
            scroll: usize::MAX,
        };
        let sse = if let Some(m) = self.data.machines.iter().find(|m| m.name == a.machine) {
            if !m.host.is_empty() {
                let rx = spawn_sse_watch(&m.host, &a.id, 0);
                WatchSseState {
                    rx,
                    lines: Vec::new(),
                    last_event_id: 0,
                    fail_count: 0,
                    first_fail_at: None,
                    done: false,
                    host: m.host.clone(),
                    pending_tail: String::new(),
                    line_times: Vec::new(),
                    current_turn: 0,
                }
            } else {
                make_local_sse_state(&a.id)
            }
        } else {
            make_local_sse_state(&a.id)
        };
        if self.watch_pool.len() >= WATCH_POOL_CAP {
            let lru_id = self
                .watch_pool
                .iter()
                .min_by_key(|(_, ctx)| ctx.last_focused_at)
                .map(|(id, _)| id.clone());
            if let Some(id) = lru_id {
                self.watch_pool.remove(&id);
            }
        }
        self.watch_pool.insert(
            aid,
            WatchContext {
                state,
                sse,
                inject_transcript: Vec::new(),
                inject_sse_offsets: Vec::new(),
                history_turns: Vec::new(),
                last_focused_at: Instant::now(),
            },
        );
    }

    /// Force a fresh SSE connection for the focused watch session (R key).
    ///
    /// Replaces the existing `WatchSseState` with a fresh connection starting
    /// from byte offset 0, so the full log is streamed from the beginning.
    fn reset_sse_watch(&mut self) {
        let id = match &self.watch_focused {
            Some(id) => id.clone(),
            None => return,
        };
        let (host, machine) = match self.watch_pool.get(&id) {
            Some(ctx) => (ctx.sse.host.clone(), ctx.state.machine.clone()),
            None => return,
        };
        // If we don't have a host yet (local machine), try to derive it.
        let host = if host.is_empty() {
            match self.data.machines.iter().find(|m| m.name == machine) {
                Some(m) if !m.host.is_empty() => m.host.clone(),
                _ => return,
            }
        } else {
            host
        };
        let rx = spawn_sse_watch(&host, &id, 0);
        if let Some(ctx) = self.watch_pool.get_mut(&id) {
            ctx.sse = WatchSseState {
                rx,
                lines: Vec::new(),
                last_event_id: 0,
                fail_count: 0,
                first_fail_at: None,
                done: false,
                host,
                pending_tail: String::new(),
                line_times: Vec::new(),
                current_turn: 0,
            };
        }
    }

    // ── Focused-context accessors ─────────────────────────────────────────────

    /// Borrow the `WatchState` for the focused session, if any.
    fn focused_watch_state(&self) -> Option<&WatchState> {
        self.watch_focused
            .as_ref()
            .and_then(|id| self.watch_pool.get(id))
            .map(|ctx| &ctx.state)
    }

    /// Mutably borrow the `WatchState` for the focused session, if any.
    fn focused_watch_state_mut(&mut self) -> Option<&mut WatchState> {
        let id = self.watch_focused.clone()?;
        self.watch_pool.get_mut(&id).map(|ctx| &mut ctx.state)
    }

    /// Borrow the inject transcript for the focused session (empty slice if none).
    fn focused_transcript(&self) -> &[ChatTurn] {
        self.watch_focused
            .as_ref()
            .and_then(|id| self.watch_pool.get(id))
            .map(|ctx| ctx.inject_transcript.as_slice())
            .unwrap_or(&[])
    }

    /// Accept the plan being watched: dispatches `coord approve-plan <id>`.
    /// No-op when the watched assignment isn't a plan; toasts otherwise.
    fn approve_watched_plan(&mut self) -> bool {
        let (aid, issue_number, atype) = match self.focused_watch_state() {
            Some(w) => (
                w.assignment_id.clone(),
                w.issue_number,
                w.assignment_type.clone(),
            ),
            None => return false,
        };
        if atype != "plan" {
            self.pipeline_status = Some((
                "approve only works on a plan-type assignment".to_string(),
                Instant::now(),
            ));
            return false;
        }
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&["approve-plan", &aid]);
        match outcome {
            SpawnQueuedOutcome::Started => {
                self.pipeline_status = Some((
                    format!("approving plan #{} → dispatching work", issue_number),
                    Instant::now(),
                ));
                // Close the watch so the user returns to Stages and can see the
                // new work assignment appear on the next refresh.
                self.close_watch();
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "approve-plan runs after current command",
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Deduped => {}
        }
        matches!(
            outcome,
            SpawnQueuedOutcome::Started | SpawnQueuedOutcome::Queued
        )
    }

    /// Find a running or non-done assignment for the currently-selected
    /// Pipeline row and dispatch `coord stop <id>` for it.  Returns
    /// `true` when a stop was dispatched, `false` when no candidate
    /// assignment was found (the caller toasts the user).
    fn dispatch_stop_for_selected_pipeline_row(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
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
        let Some(a) = pick else {
            return false;
        };
        let aid = a.id.clone();
        let issue_n = a.issue_number;
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&["stop", &aid]);
        match outcome {
            SpawnQueuedOutcome::Started => {
                self.pipeline_status =
                    Some((format!("stop dispatched for #{}", issue_n), Instant::now()));
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "stop runs after current command",
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Deduped => {}
        }
        matches!(
            outcome,
            SpawnQueuedOutcome::Started | SpawnQueuedOutcome::Queued
        )
    }

    /// Open the PR for the currently-selected Pipeline row in the user's
    /// default browser via `gh pr view --web`.  Returns `true` when the
    /// child was spawned; `false` when no PR has been opened yet (the
    /// merge_queue entry has no `pr_number`).
    fn dispatch_open_pr_for_selected_pipeline_row(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
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
                "pr",
                "view",
                &pr_number.to_string(),
                "--repo",
                &issue.repo_slug,
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
        let (aid, issue_number) = match self.focused_watch_state() {
            Some(w) => (w.assignment_id.clone(), w.issue_number),
            None => return false,
        };
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&["stop", &aid]);
        match outcome {
            SpawnQueuedOutcome::Started => {
                self.pipeline_status = Some((
                    format!("stop dispatched for #{}", issue_number),
                    Instant::now(),
                ));
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "stop runs after current command",
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Deduped => {}
        }
        matches!(
            outcome,
            SpawnQueuedOutcome::Started | SpawnQueuedOutcome::Queued
        )
    }

    /// Build the body `ListView` for the watch overlay — the raw log lines
    /// from the worker.  Title carries the repo/issue/machine context.
    /// Inject prompt (when open) is rendered as the last list row.
    ///
    /// Log content is driven by the SSE stream when the focused context has
    /// a live stream; falls back to the polling path (`get_activity_log`)
    /// when SSE is unavailable (e.g. local assignment, no host).
    fn watch_log_list(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();
        let ctx = self
            .watch_focused
            .as_ref()
            .and_then(|id| self.watch_pool.get(id));
        let title = match ctx {
            None => " WATCH ".to_string(),
            Some(ctx) => {
                let w = &ctx.state;
                let sse = &ctx.sse;
                // SSE stream: show accumulated lines or "Connecting…" placeholder.
                if !sse.done || !sse.lines.is_empty() {
                    if sse.lines.is_empty() && !sse.done {
                        items.push(kv_item(
                            "",
                            "  Connecting to log stream…",
                            Some(Color::rgb(140, 140, 140)),
                        ));
                    } else {
                        // Use the readable renderer (#385) so the live watch view
                        // word-wraps prose and shows arrow-prefixed tool calls —
                        // same format as the Log tab and `coord log` output.
                        let wrap_width = self.last_log_panel_cols.get().max(40);
                        items.extend(parse_sse_log_readable(
                            &sse.lines,
                            &sse.line_times,
                            wrap_width,
                        ));
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
                    " WATCH — {} #{} → {} ({}) (b=chat{}  K=kill  R=refresh  q=close) ",
                    w.repo, w.issue_number, w.machine, w.assignment_type, extra_keys
                )
            }
        };
        // Stick-to-bottom default: position the viewport so the LAST item
        // is the last visible row. The viewport-row count is cached during
        // the most recent mouse_main_scroll / tick (see
        // `last_main_visible_rows`); on the very first frame it defaults to
        // 40 so this fits a typical terminal. The hard-coded 40 used to be
        // a literal `items.len() - 40`, which clipped the latest lines on
        // smaller terminals.
        let visible_rows = self.last_main_visible_rows.get().max(1);
        let scroll = self
            .watch_focused
            .as_ref()
            .and_then(|id| self.watch_pool.get(id))
            .map(|ctx| {
                if ctx.state.scroll == usize::MAX {
                    items.len().saturating_sub(visible_rows)
                } else {
                    ctx.state.scroll
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
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    /// Send `text` to the watched worker by POSTing to the agent's
    /// `/inject/{id}` endpoint directly.  Bypasses the single-slot
    /// `command_runner` so chat submits aren't blocked by the periodic
    /// `coord notify` (which fires every 30 s while any assignment is
    /// running and otherwise gives a guaranteed "another command is
    /// running" lockout every cycle for a long-lived refinement chat).
    /// Appends the text to the inject transcript and clears the chat
    /// input on success.
    ///
    /// Convenience wrapper that defaults to showing the "Steer sent"
    /// confirmation toast — appropriate for user-typed submits.  Internal
    /// callers like [`Self::trigger_refinement_notes_synth`] that want a
    /// silent send (they emit their own tailored toasts) should call
    /// [`Self::submit_inject_with_toast`] with `show_toast = false`
    /// instead.
    fn submit_inject(&mut self, text: String) -> bool {
        self.submit_inject_with_toast(text, true)
    }

    /// Implementation of [`Self::submit_inject`] with explicit control
    /// over the "Steer sent" confirmation toast.
    fn submit_inject_with_toast(&mut self, text: String, show_toast: bool) -> bool {
        let (aid, issue_number, machine_name, old_type) = match self.focused_watch_state() {
            Some(w) => (
                w.assignment_id.clone(),
                w.issue_number,
                w.machine.clone(),
                w.assignment_type.clone(),
            ),
            None => return false,
        };
        let text = text.trim().to_string();
        if text.is_empty() {
            return false;
        }
        // #264: claude -p exits after `stop_reason: end_turn` (it doesn't
        // stay alive waiting for more injects), so once the assignment is
        // in `done` state any new POST /inject/{id} will return 410
        // BrokenPipeError.
        //
        // #315: Instead of refusing, fall back to `coord chat-continue` which
        // re-dispatches the worker with `--resume <session_id>` so it loads
        // the full prior conversation before seeing this message as the next
        // user turn.  The TUI polls for the new assignment row and rebinds the
        // chat overlay to it via `maybe_bind_pending_resume`.
        //
        // We OR two signals: (1) `data.assignments` status — authoritative
        // but updated only on the coordinator's notify cycle (seconds of
        // lag), and (2) `sse.done` — flips the instant the SSE stream
        // closes, which happens immediately when the worker exits.  Without
        // the SSE check, a user submit within the few-second lag window
        // after `end_turn` would slip through the worker_done gate and hit
        // the agent's `/inject/{id}` endpoint, which returns HTTP 409
        // ("assignment is `done`, not running") and the message was lost.
        let worker_done = self.data.assignments.iter().any(|a| {
            a.id == aid && (a.status == "done" || a.status == "failed" || a.status == "cancelled")
        }) || self
            .watch_pool
            .get(&aid)
            .map(|ctx| ctx.sse.done)
            .unwrap_or(false);
        // Guard against double-dispatch: if a resume is already in flight,
        // refuse new submits with a hint rather than firing a second
        // chat-continue against the same session (which would race the
        // first and confuse claude's session storage).
        if worker_done && self.pending_chat_resume.is_some() {
            self.pipeline_status = Some((
                "⏳ Resume in flight — wait for assistant reply before sending again".to_string(),
                Instant::now(),
            ));
            return false;
        }
        if worker_done {
            // Record the user turn in the transcript before the shell-out so
            // the typed text is visible immediately and can't disappear even
            // if the dispatch takes a moment.
            let sse_offset_at_send = self
                .watch_pool
                .get(&aid)
                .map(|ctx| ctx.sse.lines.len())
                .unwrap_or(0);
            {
                let now = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .ok()
                    .map(|d| d.as_secs_f64());
                let turn = ChatTurn {
                    role: ChatRole::User,
                    text: StyledText::plain(text.clone()),
                    timestamp_unix: now,
                    line_scales: Vec::new(),
                };
                if let Some(ctx) = self.watch_pool.get_mut(&aid) {
                    ctx.inject_transcript.push(turn);
                    ctx.inject_sse_offsets.push(sse_offset_at_send);
                }
            }
            // Clear the input field.
            if let Some(ref mut chat) = self.inject_chat {
                chat.clear_input();
            }
            // Dispatch the resume in a background thread.
            let config_path = self.command_runner.config_path.clone();
            let arm_unix_secs = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0);
            spawn_chat_continue(config_path, aid.clone(), text.clone());
            // Arm the pending-resume poll so the overlay rebinds on the next
            // data refresh that surfaces the new assignment row.
            self.pending_chat_resume = Some(PendingChatResume {
                old_assignment_id: aid.clone(),
                issue_number,
                dispatched_at: Instant::now(),
                arm_unix_secs,
                old_type: Some(old_type),
            });
            self.pipeline_status =
                Some((format!("⏳ Resuming chat #{issue_number}…"), Instant::now()));
            return true;
        }
        let host = match self.data.machines.iter().find(|m| m.name == machine_name) {
            Some(m) if !m.host.is_empty() => m.host.clone(),
            _ => {
                self.pipeline_status = Some((
                    format!("no host for machine {}", machine_name),
                    Instant::now(),
                ));
                return false;
            }
        };
        spawn_inject_post(
            &host,
            &aid,
            &text,
            issue_number,
            self.inject_fallback_tx.clone(),
        );
        // Optimistic toast: fire-and-forget; 409/410 will fall back via
        // inject_fallback_rx, so we surface the confirmation immediately
        // rather than waiting for the HTTP round-trip.  Use "Steer sent"
        // (not "delivered") since network errors are silently dropped.
        //
        // Suppressed for internal callers (e.g. refinement-notes synth)
        // that emit their own tailored toasts — the user pressed Ctrl+N,
        // not the steer keybind, so "Steer sent" would be misleading.
        if show_toast {
            self.push_toast(
                "Steer sent",
                &format!("Message sent to worker #{}", issue_number),
                ToastSeverity::Info,
            );
        }
        // Stamp activity NOW so the busy spinner appears the instant the
        // user sends — without this we'd wait for the first SSE byte to
        // come back (which can be several seconds for the model's
        // first-token latency).
        self.chat_last_activity = Some(Instant::now());
        // Capture the SSE stream position at submit time so the transcript
        // can interleave this user turn between the right pair of
        // assistant turns when it rebuilds.  Without this the user turns
        // all pile at the top of the transcript regardless of when they
        // were sent (the bug the user reported: "my message goes above
        // the ai messages not inline in order").
        let sse_offset_at_send = self
            .watch_pool
            .get(&aid)
            .map(|ctx| ctx.sse.lines.len())
            .unwrap_or(0);
        {
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .ok()
                .map(|d| d.as_secs_f64());
            let turn = ChatTurn {
                role: ChatRole::User,
                text: StyledText::plain(text.clone()),
                timestamp_unix: now,
                line_scales: Vec::new(),
            };
            if let Some(ctx) = self.watch_pool.get_mut(&aid) {
                ctx.inject_transcript.push(turn);
                ctx.inject_sse_offsets.push(sse_offset_at_send);
            }
            // #264: build the chat transcript by merging user turns from
            // inject_transcript with assistant turns parsed from the SSE
            // stream-json lines, so the chat shows both sides of the
            // conversation.  The previous behaviour (user turns only)
            // worked for worker-guidance chat — the user assumed the
            // assistant was "doing the work" via the Log tab — but for
            // refinement-chat the assistant's reply IS the deliverable.
            let transcript = self
                .focused_watch_state()
                .map(|w| w.assignment_id.clone())
                .and_then(|id| self.watch_pool.get(&id))
                .map(chat_transcript_from_pool)
                .unwrap_or_else(|| self.focused_transcript().to_vec());
            if let Some(ref mut chat) = self.inject_chat {
                chat.set_transcript(transcript);
                chat.clear_input();
            }
            self.pipeline_status = Some((
                format!("asked worker #{}: {}", issue_number, text),
                Instant::now(),
            ));
        }
        true
    }

    /// Spawn `coord sync --quiet` in the background if not already running
    /// and the last sync was more than 5 minutes ago (or never run).
    fn kick_issue_sync(&mut self) {
        const SYNC_INTERVAL: Duration = Duration::from_secs(300);
        // #584: a thin client must not auto-run `coord sync` (host-side; it
        // writes the issues cache to the local DB and needs a config).
        if is_remote_board_service() {
            return;
        }
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

    /// Force-run `coord sync` immediately, bypassing the 5-minute guard.
    /// Used by the Sync toolbar button and the `S` keybind in the Board panel.
    fn force_issue_sync(&mut self) {
        use crate::commands::SpawnQueuedOutcome;
        match self.command_runner.spawn_queued(&["sync", "--quiet"]) {
            SpawnQueuedOutcome::Started => {
                self.issue_sync_last = Some(Instant::now());
                self.push_toast(
                    "Sync",
                    "Fetching open issues from GitHub…",
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "Sync",
                    "Sync queued — will run after current command.",
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Deduped => {
                // A sync is already running or already queued — nothing to do.
            }
        }
    }

    /// Drain any completed background data load, applying results to
    /// `self.data`.  Returns `true` if data was updated (caller should
    /// trigger a redraw).
    /// #486: build the Pipeline issue list from the local DB cache
    /// (`data.open_issues`, kept fresh by the background `coord sync`) instead
    /// of a live `gh search`.  Same source the Board uses — instant, no Search
    /// API rate limit (30/min), no eventually-consistent index lag.  An issue
    /// is in the Pipeline iff it carries one of the tracked labels (e.g.
    /// `coord`); classification into New/Active/Done is done downstream by
    /// `pipeline_lifecycle_section` against `data.assignments` / `merge_queue`.
    fn pipeline_issues_from_cache(&self) -> Vec<PipelineIssue> {
        let tracked: std::collections::HashSet<&str> = self
            .data
            .pipeline_tracked_labels
            .iter()
            .map(|s| s.as_str())
            .collect();
        if tracked.is_empty() {
            return Vec::new();
        }
        // coord-local repo name → github slug (for PipelineIssue.repo_slug).
        let slug_of: std::collections::HashMap<&str, &str> = self
            .data
            .pipeline_repos
            .iter()
            .map(|(local, slug)| (local.as_str(), slug.as_str()))
            .collect();
        let mut issues: Vec<PipelineIssue> = self
            .data
            .open_issues
            .iter()
            .filter_map(|oi| {
                let matched: Vec<String> = oi
                    .labels
                    .iter()
                    .filter(|l| tracked.contains(l.as_str()))
                    .cloned()
                    .collect();
                if matched.is_empty() {
                    return None; // not tracked → not in the Pipeline
                }
                let repo_slug = slug_of
                    .get(oi.repo_name.as_str())
                    .map(|s| s.to_string())
                    .unwrap_or_else(|| oi.repo_name.clone());
                Some(PipelineIssue {
                    number: oi.number,
                    title: oi.title.clone(),
                    body: oi.body.clone(),
                    repo_slug,
                    coord_repo: Some(oi.repo_name.clone()),
                    matched_labels: matched,
                    all_labels: oi.labels.clone(),
                    is_closed: oi.state == "closed",
                })
            })
            .collect();
        // Stable order: by repo, then issue number (mirrors the old loader).
        issues.sort_by(|a, b| a.repo_slug.cmp(&b.repo_slug).then(a.number.cmp(&b.number)));
        issues
    }

    fn apply_pending_data(&mut self) -> bool {
        let rx = match &self.pending_data {
            Some(rx) => rx,
            None => return false,
        };
        match rx.try_recv() {
            Ok(data) => {
                // #620: a refresh tick that comes back completely empty is
                // almost always a transient fetch FAILURE, not a genuinely
                // empty board.  `load_data_remote` returns `BoardData::default()`
                // on any connect/timeout/parse error (thin clients poll the
                // dellserver daemon over Tailscale, so an 8s timeout or a blip
                // yields an empty payload), and the local SQLite path returns it
                // when the DB is briefly locked.  Wholesale-applying that empty
                // payload wiped the Pipeline tree, recomputed an empty
                // `live_keys`, and `retain`-ed every live embedded terminal out
                // of existence — bouncing the operator out of an attended
                // session every couple of minutes and losing selection +
                // tree-expansion state.  Guard: if we ALREADY have data and the
                // incoming tick has no machines, no issues, AND no assignments,
                // treat it as a degraded tick — keep the last good data, surface
                // a soft self-clearing warning, and skip the apply.  (A real
                // first load from an empty board has empty CURRENT data, so the
                // guard is inert and the empty state still applies; machines come
                // from coordinator.yml and never legitimately drop to zero on a
                // configured board, making the all-three-empty check a reliable
                // proxy for the default sentinel.)
                let incoming_empty = data.machines.is_empty()
                    && data.open_issues.is_empty()
                    && data.assignments.is_empty();
                let have_data = !self.data.machines.is_empty()
                    || !self.data.open_issues.is_empty()
                    || !self.data.assignments.is_empty();
                if incoming_empty && have_data {
                    // Consume the receiver but do NOT replace self.data; leave
                    // refreshed_at alone so the "Xs ago" indicator keeps aging
                    // (honest: this tick didn't land).  The next healthy tick
                    // clears the warning and updates normally.
                    self.pending_data = None;
                    self.fetch_error = Some((
                        "refresh failed — showing last good data".into(),
                        Instant::now(),
                    ));
                    return true;
                }

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
                // #290: clear optimistic merge-in-flight flags for issues where
                // the DB has now confirmed a real merge_queue entry (any state).
                // Once the row exists, merge_stage_status_for reads the real state.
                self.pipeline_inflight_merges.retain(|(_, issue_number)| {
                    !self
                        .data
                        .merge_queue
                        .iter()
                        .any(|m| m.issue_number == Some(*issue_number))
                });
                let m = self.data.machines.len();
                if m > 0 {
                    self.machine_sel = self.machine_sel.min(m - 1);
                } else {
                    self.machine_sel = 0;
                }
                // Update last-contact timestamps for machines whose health
                // fetch succeeded this cycle (version is Some).
                for machine in &self.data.machines {
                    if machine.version.is_some() {
                        self.machine_last_contact
                            .insert(machine.name.clone(), Instant::now());
                    }
                }
                self.rebuild_board_sidebar();
                // #486: the Pipeline now sources its issue list from the same
                // DB cache the Board uses (data.open_issues), rebuilt on every
                // data tick — no live gh search.  Capture the selection first so
                // replacing pipeline_issues doesn't jump the cursor to row 0.
                let prev_pl_sel = self.capture_pipeline_selection_id();
                self.pipeline_issues = self.pipeline_issues_from_cache();
                // Prune terminal sessions / spawn errors for issues no longer
                // in the pipeline or on the board (mirrors the old loader's cleanup).
                let mut live_keys: std::collections::HashSet<(String, u64)> = self
                    .pipeline_issues
                    .iter()
                    .map(|i| (i.repo_slug.clone(), i.number))
                    .collect();
                // #675 / follow-up: Board Terminal sessions must survive even when
                // their issue is visible on the Board ONLY via assignment history
                // (no open_issues record — coord sync hasn't run yet for this
                // repo, or the assignment was created directly via `coord assign`
                // before the first sync).  The d3dcd39 fix only iterated
                // data.open_issues, which misses assignment-only Board entries.
                // Use board_issues_cache instead — it was just rebuilt above by
                // rebuild_board_sidebar() and includes BOTH open_issues-based
                // entries AND assignment-only entries, exactly matching the full
                // set of issues visible on the Board.  A session is pruned only
                // when its issue is gone from BOTH the pipeline AND the board.
                for (repo, groups) in &self.board_issues_cache {
                    let slug = self
                        .data
                        .pipeline_repos
                        .iter()
                        .find(|(name, _)| name.as_str() == repo.as_str())
                        .map(|(_, s)| s.clone())
                        .unwrap_or_else(|| repo.clone());
                    for group in groups {
                        live_keys.insert((slug.clone(), group.issue_number));
                    }
                }
                // #620: never prune EVERY terminal at once.  The top-level
                // degraded-tick guard above catches a fully-empty payload, but a
                // *partial* degradation (machines/issues present yet
                // `pipeline_tracked_labels` momentarily empty from a board_meta
                // hiccup) still yields an empty `pipeline_issues` → empty
                // `live_keys` → the retain would drop every live embedded
                // terminal and bounce the operator.  An empty live set while we
                // still hold terminals is itself a near-certain degradation, so
                // skip the prune this tick; stale terminals are harmless and get
                // pruned on the next healthy tick (when live_keys is non-empty).
                let prune_terminals =
                    !live_keys.is_empty() || self.detail_terminal_sessions.is_empty();
                if prune_terminals {
                    self.detail_terminal_sessions.retain(|k, _| live_keys.contains(k));
                }
                self.detail_terminal_spawn_errors.retain(|k, _| live_keys.contains(k));
                self.rebuild_pipeline_sidebar(prev_pl_sel);

                // Ring the terminal bell (BEL) when an assignment that was
                // running is now done or failed, if the user enabled audio.
                if self.settings.audio_on_completion && !prev_running.is_empty() {
                    let newly_finished = self.data.assignments.iter().any(|a| {
                        (a.status == "done" || a.status == "failed") && prev_running.contains(&a.id)
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

                // #349: Reset staleness-check tracking so the next tick re-runs
                // the git HEAD comparison.  New data may reflect new commits so
                // we want to re-check even for the same work assignment.
                self.test_plan_staleness_checked_for = None;

                // #349: Clear pending-spawn entries for work assignments whose
                // plan has now landed (test_plan is Some after the refresh).
                // Without this, the pending set would grow forever and the
                // "Preparing plan…" label might linger after the plan is ready.
                let newly_planned: Vec<String> = self
                    .test_plan_pending
                    .iter()
                    .filter(|id| {
                        self.data
                            .assignments
                            .iter()
                            .any(|a| &a.id == *id && a.test_plan.is_some())
                    })
                    .cloned()
                    .collect();
                for id in newly_planned {
                    self.test_plan_pending.remove(&id);
                }

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
                    milestone_number: None,
                    milestone_title: None,
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
                    // #406: copy milestone from the issues cache onto the group.
                    // The open_issues row is always the authority — overwrite even
                    // if a previous assignment had already stamped a value.
                    group.milestone_number = oi.milestone_number;
                    group.milestone_title = oi.milestone_title.clone();
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
                milestone_number: oi.milestone_number,
                milestone_title: oi.milestone_title.clone(),
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
            has_active(&a.1)
                .cmp(&has_active(&b.1))
                .then_with(|| a.0.cmp(&b.0))
        });

        result
    }

    /// #406: Group a repo's issues by milestone, then by lifecycle status within
    /// each milestone group.
    ///
    /// Returns `Vec<(milestone_key, display_title, status_groups)>` where:
    /// - `milestone_key` — `"<n>"` for numbered milestones (e.g. `"5"`), or
    ///   `"no-milestone"` for unassigned issues.
    /// - `display_title` — the milestone title (e.g. `"v0.5"`) or
    ///   `"No milestone"`.
    /// - `status_groups` — `Vec<(display_name, key, issues)>` in display order
    ///   (Backlog → Refining → Refined → In-flight → Completed), with empty
    ///   groups omitted and the `board_search` filter applied.
    ///
    /// Sorted: named milestones by number ASC, `"No milestone"` last.
    ///
    /// Production rendering uses `board_milestones_for_repo` (flat, no status
    /// sub-groups).  This function is retained for tests that verify the
    /// status-bucketing logic.
    #[allow(dead_code)]
    fn board_milestones_with_status_for_repo<'a>(
        &'a self,
        issues: &'a [(String, Vec<IssueGroup>)],
        repo: &str,
    ) -> Vec<(
        String,
        String,
        Vec<(&'static str, &'static str, Vec<(usize, &'a IssueGroup)>)>,
    )> {
        let flat: &[IssueGroup] = match issues.iter().find(|(r, _)| r == repo) {
            Some((_, v)) => v,
            None => return Vec::new(),
        };

        // Bucket flat indices into milestones.
        let mut milestone_map: std::collections::BTreeMap<
            (i64, String), // (number, title) for sorting; number=i64::MAX for "no-milestone"
            (String, String, Vec<usize>), // (key, display_title, flat_indices)
        > = std::collections::BTreeMap::new();

        for (flat_idx, g) in flat.iter().enumerate() {
            if !self.board_search.matches(g.issue_number, &g.issue_title) {
                continue;
            }
            match g.milestone_number {
                Some(n) => {
                    let title = g.milestone_title.clone().unwrap_or_default();
                    let key = n.to_string();
                    let sort_key = (n, title.clone());
                    milestone_map
                        .entry(sort_key)
                        .or_insert_with(|| (key, title, Vec::new()))
                        .2
                        .push(flat_idx);
                }
                None => {
                    let sort_key = (i64::MAX, String::new());
                    milestone_map
                        .entry(sort_key)
                        .or_insert_with(|| {
                            (
                                "no-milestone".to_string(),
                                "No milestone".to_string(),
                                Vec::new(),
                            )
                        })
                        .2
                        .push(flat_idx);
                }
            }
        }

        // For each milestone, build the status sub-groups.
        let mut result = Vec::new();
        for (_, (key, display_title, flat_indices)) in milestone_map {
            let mut backlog: Vec<(usize, &IssueGroup)> = Vec::new();
            let mut refining: Vec<(usize, &IssueGroup)> = Vec::new();
            let mut refined: Vec<(usize, &IssueGroup)> = Vec::new();
            let mut in_flight: Vec<(usize, &IssueGroup)> = Vec::new();
            let mut completed: Vec<(usize, &IssueGroup)> = Vec::new();
            for fi in &flat_indices {
                let g = &flat[*fi];
                match g.lifecycle_section() {
                    "backlog" => backlog.push((*fi, g)),
                    "refining" => refining.push((*fi, g)),
                    "refined" => refined.push((*fi, g)),
                    "in-flight" => in_flight.push((*fi, g)),
                    "completed" => completed.push((*fi, g)),
                    _ => backlog.push((*fi, g)),
                }
            }
            let status_groups: Vec<(&'static str, &'static str, Vec<(usize, &IssueGroup)>)> = [
                ("Backlog", "backlog", backlog),
                ("Refining", "refining", refining),
                ("Refined", "refined", refined),
                ("In-flight", "in-flight", in_flight),
                ("Completed", "completed", completed),
            ]
            .into_iter()
            .filter(|(_, _, v)| !v.is_empty())
            .collect();

            if !status_groups.is_empty() {
                result.push((key, display_title, status_groups));
            }
        }
        result
    }

    /// #410: Group a repo's issues by milestone only (no status sub-grouping).
    ///
    /// Returns `Vec<(milestone_key, display_title, issues)>` where:
    /// - `milestone_key` — `"<n>"` for numbered milestones (e.g. `"5"`), or
    ///   `"no-milestone"` for unassigned issues.
    /// - `display_title` — milestone title or `"No milestone"`.
    /// - `issues` — `Vec<(flat_idx, &IssueGroup)>` in board order with the
    ///   `board_search` filter applied.
    ///
    /// Sorted: named milestones by number ASC, `"No milestone"` last.
    fn board_milestones_for_repo<'a>(
        &'a self,
        issues: &'a [(String, Vec<IssueGroup>)],
        repo: &str,
    ) -> Vec<(String, String, Vec<(usize, &'a IssueGroup)>)> {
        let flat: &[IssueGroup] = match issues.iter().find(|(r, _)| r == repo) {
            Some((_, v)) => v,
            None => return Vec::new(),
        };

        let mut milestone_map: std::collections::BTreeMap<
            (i64, String),
            (String, String, Vec<usize>),
        > = std::collections::BTreeMap::new();

        for (flat_idx, g) in flat.iter().enumerate() {
            if !self.board_search.matches(g.issue_number, &g.issue_title) {
                continue;
            }
            match g.milestone_number {
                Some(n) => {
                    let title = g.milestone_title.clone().unwrap_or_default();
                    let key = n.to_string();
                    let sort_key = (n, title.clone());
                    milestone_map
                        .entry(sort_key)
                        .or_insert_with(|| (key, title, Vec::new()))
                        .2
                        .push(flat_idx);
                }
                None => {
                    let sort_key = (i64::MAX, String::new());
                    milestone_map
                        .entry(sort_key)
                        .or_insert_with(|| {
                            (
                                "no-milestone".to_string(),
                                "No milestone".to_string(),
                                Vec::new(),
                            )
                        })
                        .2
                        .push(flat_idx);
                }
            }
        }

        milestone_map
            .into_values()
            .filter(|(_, _, idxs)| !idxs.is_empty())
            .map(|(key, display_title, idxs)| {
                let group_issues: Vec<(usize, &IssueGroup)> =
                    idxs.into_iter().map(|fi| (fi, &flat[fi])).collect();
                (key, display_title, group_issues)
            })
            .collect()
    }

    /// Rebuild the SidebarSystem from current data.
    ///
    /// Layout:
    /// - Section 0: search form (always present)
    /// - Section 1: PROPOSALS (only when proposals exist)
    /// - Section 1/2+: one section per repo
    ///
    /// Within each repo section, issues are grouped by milestone then issue
    /// (Repo > Milestone > Issue).  Each issue row carries a trailing
    /// status letter (R/A/D) as a right-aligned badge.  Empty groups are
    /// omitted.  Rows are filtered by `board_search` (case-insensitive
    /// substring).
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
            let mut def =
                SidebarSectionDef::new("section:proposals".to_string(), "PROPOSALS".to_string());
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
        self.board_sidebar
            .set_navigation_mode(NavigationMode::Selection);
        self.board_sidebar.set_allow_collapse(true);
        self.board_sidebar.set_scroll_mode(ScrollMode::WholePanel);

        // Populate search form (section 0).
        self.board_sidebar
            .set_form(0, self.board_search.form("board-search", "Filter issues…"));

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
                            StyledSpan::with_fg(
                                format!("{} ", p.machine),
                                Color::rgb(140, 200, 140),
                            ),
                            StyledSpan::with_fg(
                                format!("#{} ", p.issue_number),
                                Color::rgb(150, 150, 240),
                            ),
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
                Some(StyledText::plain(format!(
                    "({})",
                    self.data.proposals.len()
                ))),
            );
        }

        // #410: Build per-repo milestone > issue rows (status sub-group removed).
        //
        // Two-phase loop to satisfy the borrow checker:
        // Phase 1 — inside the block, compute `(total, rows)` using
        //   `board_milestones_for_repo` (borrows &self).
        //   `milestones` is dropped at the end of the block.
        // Phase 2 — outside the block, apply mutations to `self.board_sidebar`
        //   (requires &mut self, which is safe once milestones is dropped).
        for (cache_idx, (repo, _issues)) in grouped.iter().enumerate() {
            let section_idx = cache_idx + offset;

            let (total, rows): (usize, Vec<TreeRow>) = {
                // Compute milestone-grouped data for this repo.
                // milestones holds &IssueGroup refs; it must be dropped before
                // any &mut self borrow (sidebar mutations) below.
                let milestones = self.board_milestones_for_repo(grouped, repo);

                let total: usize = milestones.iter().map(|(_, _, v)| v.len()).sum();

                let mut rows: Vec<TreeRow> = Vec::new();

                for (milestone_idx, (m_key, m_display, group_issues)) in
                    milestones.iter().enumerate()
                {
                    let mi = milestone_idx as u16;

                    let m_has_inflight = group_issues
                        .iter()
                        .any(|(_, g)| g.lifecycle_section() == "in-flight");

                    // Expand state: default to expanded when in-flight, else collapsed.
                    let m_is_exp = *self
                        .board_milestone_expanded
                        .get(&(repo.clone(), m_key.clone()))
                        .unwrap_or(&m_has_inflight);

                    // Milestone header color.
                    let m_header_color = if m_has_inflight {
                        Color::rgb(200, 200, 100)
                    } else if m_key == "no-milestone" {
                        Color::rgb(100, 100, 120)
                    } else {
                        Color::rgb(160, 160, 200)
                    };

                    rows.push(TreeRow {
                        path: vec![mi],
                        indent: 1,
                        icon: None,
                        text: StyledText {
                            spans: vec![StyledSpan::with_fg(
                                format!("{} ({})", m_display, group_issues.len()),
                                m_header_color,
                            )],
                        },
                        badge: None,
                        is_expanded: Some(m_is_exp),
                        decoration: Decoration::Header,
                        edit: None,
                    });

                    if m_is_exp {
                        for (issue_idx, (_flat_idx, g)) in group_issues.iter().enumerate() {
                            let text = StyledText {
                                spans: vec![
                                    StyledSpan::with_fg(
                                        format!("#{:<5}", g.issue_number),
                                        Color::rgb(150, 150, 240),
                                    ),
                                    StyledSpan::plain(trunc(&g.issue_title, 20)),
                                ],
                            };
                            // #410: per-row status letter badge (R/A/D).
                            let badge = board_row_status_badge(g.lifecycle_section());
                            rows.push(TreeRow {
                                path: vec![mi, issue_idx as u16],
                                indent: 2,
                                icon: None,
                                text,
                                badge,
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
                // milestones dropped here — &self borrow released.
                (total, rows)
            };

            // Phase 2: apply mutations now that milestones is dropped.
            if total > 0 {
                self.board_sidebar.set_section_badge(
                    section_idx,
                    Some(StyledText::plain(format!("({})", total))),
                );
            }
            if total == 0 {
                self.board_sidebar.set_collapsed(section_idx, true);
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
                    self.board_sidebar
                        .set_collapsed(search_offset, was_collapsed);
                }
            }
            let new_names: Vec<String> = self.board_repo_names.clone();
            for (i, name) in new_names.into_iter().enumerate() {
                if let Some(&was_collapsed) = prev_collapsed.get(&name) {
                    self.board_sidebar
                        .set_collapsed(i + new_offset, was_collapsed);
                }
            }
        }
        // Re-apply filter focus after rebuild so the cursor survives auto-refresh.
        if self.board_search.focused {
            self.board_sidebar.focus_form(0, true);
        }

        // #638: Keep Kanban model in sync with new board data.
        let new_kanban = self.build_kanban_model();
        let old_sel = self.kanban_model.selected_card_id.clone();
        let old_col_scroll = self.kanban_model.col_scroll_offset;
        // Preserve per-column vertical scroll offsets so a background data
        // refresh does not snap the user's view back to the top (review issue 3).
        let old_vert_offsets: Vec<(WidgetId, usize)> = self.kanban_model.columns
            .iter()
            .map(|c| (c.id.clone(), c.scroll_offset))
            .collect();
        self.kanban_model = new_kanban;
        if let Some(sel) = &old_sel {
            let still_exists = self.kanban_model.columns.iter()
                .any(|c| c.cards.iter().any(|card| &card.id == sel));
            if still_exists {
                self.kanban_model.selected_card_id = old_sel;
            }
        }
        self.kanban_model.col_scroll_offset = old_col_scroll;
        // Restore per-column vertical scroll offsets.
        for col in &mut self.kanban_model.columns {
            if let Some((_, offset)) = old_vert_offsets.iter().find(|(id, _)| id == &col.id) {
                col.scroll_offset = *offset;
            }
        }
    }

    /// Repo section offset: 1 for the search form + 1 more if proposals exist.
    fn board_repo_offset(&self) -> usize {
        1 + if self.has_proposals_section { 1 } else { 0 }
    }

    /// #638: Build a fresh `BoardModel` from the current `board_issues_cache`.
    ///
    /// Three columns: **Backlog** (backlog + refining + refined),
    /// **In Flight** (in-flight), **Completed** (completed).
    /// Each issue group becomes one card; its repo name is prepended as a label.
    fn build_kanban_model(&self) -> BoardModel {
        let mut backlog_cards: Vec<BoardCard> = Vec::new();
        let mut inflight_cards: Vec<BoardCard> = Vec::new();
        let mut completed_cards: Vec<BoardCard> = Vec::new();

        for (repo, groups) in &self.board_issues_cache {
            for g in groups {
                let card_id = WidgetId::new(&format!("card:{}:{}", repo, g.issue_number));
                let stage_badges = kanban_stage_badges(g);
                let machine = g.assignments.iter()
                    .find(|a| a.status == "running")
                    .map(|a| a.machine.clone());
                let card = BoardCard {
                    id: card_id,
                    title: format!("#{} {}", g.issue_number, g.issue_title),
                    labels: vec![repo.clone()],
                    stage_badges,
                    assignee: None,
                    machine,
                    decision_hint: None,
                };
                match g.lifecycle_section() {
                    "in-flight" => inflight_cards.push(card),
                    "completed" => completed_cards.push(card),
                    _ => backlog_cards.push(card), // backlog / refining / refined
                }
            }
        }

        BoardModel {
            id: WidgetId::new("kanban:coord"),
            columns: vec![
                BoardColumn {
                    id: WidgetId::new("kanban-col:backlog"),
                    title: "Backlog".to_string(),
                    cards: backlog_cards,
                    scroll_offset: 0,
                },
                BoardColumn {
                    id: WidgetId::new("kanban-col:inflight"),
                    title: "In Flight".to_string(),
                    cards: inflight_cards,
                    scroll_offset: 0,
                },
                BoardColumn {
                    id: WidgetId::new("kanban-col:completed"),
                    title: "Completed".to_string(),
                    cards: completed_cards,
                    scroll_offset: 0,
                },
            ],
            selected_card_id: None,
            col_scroll_offset: 0,
        }
    }

    /// Clamp the focused column's scroll offset so the selected card is visible.
    fn kanban_clamp_col_scroll(&mut self) {
        // Compute the selected card's (col_index, card_index) before the
        // mutable borrow of kanban_model.columns below.  We replicate the
        // logic of quadraui's private `BoardModel::selected_position()`.
        let sel_pos: (Option<usize>, Option<usize>) = {
            let mut result = (None, None);
            if let Some(id) = self.kanban_model.selected_card_id.as_ref() {
                'outer: for (ci, col) in self.kanban_model.columns.iter().enumerate() {
                    for (ri, c) in col.cards.iter().enumerate() {
                        if &c.id == id {
                            result = (Some(ci), Some(ri));
                            break 'outer;
                        }
                    }
                }
            }
            result
        };
        let (sel_col, sel_card_idx) = sel_pos;

        if let Some(layout) = self.kanban_layout.borrow().as_ref() {
            for col_layout in &layout.columns {
                let ci = col_layout.col_index;
                let col = &mut self.kanban_model.columns[ci];
                let visible = col_layout.visible_cards.max(1);
                let total = col.cards.len();
                if total == 0 {
                    col.scroll_offset = 0;
                    continue;
                }
                // Scroll-follow: adjust the window so the selected card stays
                // visible.  Only applies to the column that owns the selection.
                if sel_col == Some(ci) {
                    if let Some(idx) = sel_card_idx {
                        if idx < col.scroll_offset {
                            // Selection scrolled above the top of the window.
                            col.scroll_offset = idx;
                        } else if idx >= col.scroll_offset + visible {
                            // Selection scrolled below the bottom of the window.
                            col.scroll_offset = (idx + 1).saturating_sub(visible);
                        }
                    }
                }
                // Upper-bound clamp so we never show empty rows at the bottom.
                col.scroll_offset = col.scroll_offset.min(total.saturating_sub(visible));
            }
        }
    }

    /// Open the Board view and select the issue represented by a Kanban card id.
    fn kanban_open_card(&mut self, card_id: &WidgetId) {
        // card id format: "card:<repo>:<issue_number>"
        let s = card_id.as_str();
        if let Some(colon) = s.rfind(':') {
            let right = &s[colon + 1..];
            if let Ok(num) = right.parse::<u64>() {
                let repo_part = &s[..colon];
                if let Some(repo_colon) = repo_part.rfind(':') {
                    let repo = &repo_part[repo_colon + 1..];
                    self.active_view = SidebarView::Board;
                    self.select_issue(repo, num);
                }
            }
        }
    }

    /// Empty placeholder list for the Kanban sidebar slot.
    fn kanban_sidebar_placeholder(&self) -> ListView {
        ListView {
            id: WidgetId::new("kanban-sidebar"),
            title: None,
            items: vec![],
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: false,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
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
    ///
    /// Used by tests.  Production rendering goes through
    /// `board_milestones_for_repo` instead.
    #[allow(dead_code)]
    fn board_grouped_for_repo<'a>(
        &'a self,
        issues: &'a [(String, Vec<IssueGroup>)],
        repo: &str,
    ) -> Vec<(&'static str, Vec<(usize, &'a IssueGroup)>)> {
        let (_, flat) = match issues.iter().find(|(r, _)| r == repo) {
            Some(v) => v,
            None => return Vec::new(),
        };
        let mut backlog = Vec::new();
        let mut refining = Vec::new();
        let mut refined = Vec::new();
        let mut in_flight = Vec::new();
        let mut completed = Vec::new();
        for (i, g) in flat.iter().enumerate() {
            if !self.board_search.matches(g.issue_number, &g.issue_title) {
                continue;
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
    /// Paths are two-level: `[milestone_idx, issue_idx]`.
    /// A one-level path (milestone header selected) returns `None`.
    fn board_selected_issue_group(&self) -> Option<&IssueGroup> {
        let section = self.board_sidebar.active_section()?;
        let offset = self.board_repo_offset();
        if section < offset {
            return None;
        }
        let path = self.board_sidebar.selected_path(section)?;
        // #410: issue rows are at path depth 2 — [milestone_idx, issue_idx].
        if path.len() < 2 {
            return None;
        }
        let milestone_idx = path[0] as usize;
        let issue_idx = path[1] as usize;
        let repo = self.board_repo_names.get(section - offset)?;
        let milestones = self.board_milestones_for_repo(&self.board_issues_cache, repo);
        let (_, _, group_issues) = milestones.get(milestone_idx)?;
        let (flat_idx, _) = group_issues.get(issue_idx)?;
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
        let failed = group.assignments.iter().find(|a| a.status == "failed")?;
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
        // #410: issue rows are at path depth 2 — [milestone_idx, issue_idx].
        if path.len() < 2 {
            return false;
        }
        // Check lifecycle of the actually-selected issue.
        match self.board_selected_issue_group() {
            Some(g) => g.lifecycle_section() == "completed",
            None => false,
        }
    }

    /// Try to select a specific issue in the sidebar by repo and issue number.
    fn select_issue(&mut self, repo: &str, issue_number: u64) {
        let offset = self.board_repo_offset();
        // Find the repo's section index.
        let cache_idx = match self.board_repo_names.iter().position(|r| r == repo) {
            Some(i) => i,
            None => return,
        };
        let section_idx = cache_idx + offset;
        // Clone to avoid borrow conflicts.
        let cache = self.board_issues_cache.clone();
        // #410: milestone > issue bucketing — must stay in sync with
        // `board_milestones_for_repo` and `rebuild_board_sidebar`
        // so the path this function produces matches what the click handler resolves to.
        let milestones = self.board_milestones_for_repo(&cache, repo);
        for (milestone_idx, (_, _, group_issues)) in milestones.iter().enumerate() {
            for (issue_idx, (_, g)) in group_issues.iter().enumerate() {
                if g.issue_number == issue_number {
                    self.board_sidebar.set_active_section(Some(section_idx));
                    self.board_sidebar.set_selected_path(
                        section_idx,
                        Some(vec![milestone_idx as u16, issue_idx as u16]),
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

    /// Keep the selected merge-queue entry inside the visible window.
    ///
    /// Must be called after every j/k/Home/End navigation in the Merge Queue
    /// panel.  `visible` is the number of rows the list widget can display
    /// (pass `content_visible_rows(rect, lh)` from the render path, or a
    /// reasonable constant like 10 in tests).  The quadraui list widget's
    /// `layout()` skips rows above `scroll_offset`, so `merge_queue_scroll`
    /// must track the display-index of the selected entry (accounting for
    /// milestone-group header rows that precede it in the items array).
    /// Same structural pattern as `fix_machine_scroll`.
    fn fix_merge_queue_scroll(&mut self, visible: usize) {
        if visible == 0 {
            return;
        }
        let sel = self.merge_queue_display_idx_for_sel();
        if sel < self.merge_queue_scroll {
            self.merge_queue_scroll = sel;
        } else if sel >= self.merge_queue_scroll + visible {
            self.merge_queue_scroll = sel + 1 - visible;
        }
    }

    /// Return the display-list index for the currently selected merge-queue
    /// entry, accounting for group header rows inserted before each group in
    /// the render functions.
    ///
    /// When `data.merge_plan` is non-empty (#776 path), groups are by
    /// `(repo_github, target_branch)` using **consecutive-run** counting —
    /// matching `render_merge_plan_panel` which emits a new header each time
    /// the current entry's group differs from the previous one (not just on
    /// the first occurrence of each group).  When only `data.merge_queue` is
    /// available (legacy path), groups are by `milestone_title` (first-seen).
    /// Adding the header count to `sel` gives the row index the selected
    /// entry occupies in the rendered items array.
    fn merge_queue_display_idx_for_sel(&self) -> usize {
        if !self.data.merge_plan.is_empty() {
            // #776 plan path: groups are (repo_github, target_branch).
            // Use consecutive-run counting to match render_merge_plan_panel,
            // which emits a header on every group transition — including when
            // the same repo/branch pair reappears after an intervening entry.
            let entries = &self.data.merge_plan;
            let sel = self.merge_queue_sel.min(entries.len() - 1);
            let mut headers = 0usize;
            let mut last_key: Option<(&str, &str)> = None;
            for entry in entries.iter().take(sel + 1) {
                let key = (entry.repo_github.as_str(), entry.target_branch.as_str());
                if last_key != Some(key) {
                    headers += 1;
                    last_key = Some(key);
                }
            }
            sel + headers
        } else {
            // Legacy path: groups are milestone_title (first-seen).
            let entries = &self.data.merge_queue;
            if entries.is_empty() {
                return 0;
            }
            let sel = self.merge_queue_sel.min(entries.len() - 1);
            let mut seen: Vec<Option<String>> = Vec::new();
            for entry in entries.iter().take(sel + 1) {
                if !seen.contains(&entry.milestone_title) {
                    seen.push(entry.milestone_title.clone());
                }
            }
            sel + seen.len()
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
                    format!("{} (local)", trunc(&m.name, 12))
                } else {
                    trunc(&m.name, 18).to_string()
                };

                // Version badge: green if same as local, red if older/different, gray if unknown.
                let (ver_str, ver_col) = match (&m.version, &self.local_coord_version) {
                    (Some(v), Some(local)) if v != local => {
                        (format!(" v{}", v), Color::rgb(220, 80, 80))
                    }
                    (Some(v), _) => (format!(" v{}", v), Color::rgb(80, 160, 80)),
                    (None, _) => ("".to_string(), Color::rgb(90, 90, 90)),
                };

                // Last-contact age ("12s ago") when we have a recorded time.
                let contact_str = self.machine_last_contact.get(&m.name).map(|t| {
                    let secs = t.elapsed().as_secs();
                    format!(" {}ago", fmt_dur(secs))
                });

                let mut spans = vec![
                    StyledSpan::with_fg(bullet, col),
                    StyledSpan::plain(&display_name),
                ];
                if !ver_str.is_empty() {
                    spans.push(StyledSpan::with_fg(&ver_str, ver_col));
                }
                // #pause: badge a paused machine so the user sees at a
                // glance which rows are excluded from routing.  Amber so
                // it doesn't read as a failure (machine is healthy, just
                // deliberately offline for new work).
                if self.paused_machines.contains(&m.name) {
                    spans.push(StyledSpan::with_fg(" [PAUSED]", Color::rgb(230, 180, 60)));
                }

                let text = StyledText { spans };

                // #628: live interactive sessions (tmux) are not board `running`
                // rows, so active_count misses them.  Surface them here so a
                // machine hosting detached sessions no longer reads "idle".
                let live = self.live_session_count_for_machine(&m.name);
                let detail_str = if m.active_count > 0 || live > 0 {
                    let mut parts: Vec<String> = Vec::new();
                    if m.active_count > 0 {
                        parts.push(format!("{} active", m.active_count));
                    }
                    if live > 0 {
                        parts.push(format!("◉ {} live", live));
                    }
                    parts.join(" · ")
                } else if let Some(ref age) = contact_str {
                    age.clone()
                } else {
                    "idle".to_string()
                };
                let detail_col = if live > 0 {
                    Color::rgb(150, 210, 255) // matches the status-bar live badge
                } else if m.active_count > 0 {
                    Color::rgb(80, 210, 80)
                } else if contact_str.is_some() {
                    Color::rgb(90, 110, 90)
                } else {
                    Color::rgb(90, 90, 90)
                };
                let detail = Some(StyledText {
                    spans: vec![StyledSpan::with_fg(&detail_str, detail_col)],
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
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
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
            items.push(kv_item(
                "  Issue",
                &format!("  #{}: {}", p.issue_number, p.issue_title),
                None,
            ));
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
            items.push(kv_item(
                "",
                "  a=approve  A=approve all",
                Some(Color::rgb(180, 180, 120)),
            ));
            return ListView {
                id: WidgetId::new("detail"),
                title: Some(StyledText::plain(&format!("Proposal #{}", p.id))),
                items,
                selected_idx: 0,
                scroll_offset: self.detail_scroll,
                has_focus: false,
                bordered: false,
                h_scroll: 0,
                max_content_width: None,
                show_v_scrollbar: false,
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
                        spans: vec![StyledSpan::with_fg(&header_text, Color::rgb(210, 220, 255))],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                });

                // Issue title
                items.push(kv_item(
                    "",
                    &format!("  {}", trunc(&group.issue_title, 52)),
                    None,
                ));
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
                    let sc = a.status_color(&self.active_theme);
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
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
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
            return vec![kv_item(
                "",
                "  Loading log…",
                Some(Color::rgb(140, 140, 140)),
            )];
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
        self.pending_log_fetches
            .borrow_mut()
            .insert(id.to_string(), rx);
        vec![kv_item(
            "",
            "  Loading log…",
            Some(Color::rgb(140, 140, 140)),
        )]
    }

    /// Detail panel for the selected machine: status, version, workers, disk, history.
    fn machine_detail_list(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();

        match self.data.machines.get(self.machine_sel) {
            None => {
                items.push(kv_item("", " No machine selected", None));
            }
            Some(m) => {
                // ── Header ──────────────────────────────────────────────
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

                // ── Status + last-contact age ────────────────────────────
                let last_contact_str = self
                    .machine_last_contact
                    .get(&m.name)
                    .map(|t| format!("  ({} ago)", fmt_dur(t.elapsed().as_secs())));
                let (reach_str, reach_col) = if m.reachable {
                    ("reachable".to_string(), Color::rgb(80, 210, 80))
                } else {
                    ("unreachable".to_string(), Color::rgb(220, 70, 70))
                };
                let reach_val = if let Some(ref age) = last_contact_str {
                    format!("{}{}", reach_str, age)
                } else {
                    reach_str
                };
                items.push(kv_item("Status", &reach_val, Some(reach_col)));

                // ── Agent version (red if older than local) ──────────────
                let (ver_val, ver_col) = match (&m.version, &self.local_coord_version) {
                    (Some(v), Some(local)) if v != local => (
                        format!("{}  ← local is {}", v, local),
                        Color::rgb(220, 80, 80),
                    ),
                    (Some(v), Some(_)) => (v.clone(), Color::rgb(80, 160, 80)),
                    (Some(v), None) => (v.clone(), Color::rgb(150, 150, 150)),
                    (None, _) => ("unknown".to_string(), Color::rgb(90, 90, 90)),
                };
                items.push(kv_item("Version", &ver_val, Some(ver_col)));

                // ── Worktree disk usage ──────────────────────────────────
                let wt_mb = m.worktree_bytes as f64 / (1024.0 * 1024.0);
                let wt_str = if m.worktree_bytes == 0 && !m.reachable {
                    "—".to_string()
                } else {
                    format!("{:.1} MB", wt_mb)
                };
                items.push(kv_item("Worktrees", &wt_str, None));

                // ── Local indicator ──────────────────────────────────────
                let is_local = m.name == self.data.local_machine;
                if is_local {
                    items.push(kv_item(
                        "Location",
                        "local",
                        Some(Color::rgb(100, 180, 240)),
                    ));
                }

                items.push(kv_item("", "", None)); // blank

                // ── Active workers ───────────────────────────────────────
                let active_workers: Vec<&Assignment> = self
                    .data
                    .assignments
                    .iter()
                    .filter(|a| a.machine == m.name && a.status == "running")
                    .collect();

                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(
                            format!(" ACTIVE WORKERS ({}) ", active_workers.len()),
                            Color::rgb(130, 130, 150),
                        )],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                });

                if active_workers.is_empty() {
                    items.push(kv_item("", "  idle", Some(Color::rgb(80, 80, 80))));
                } else {
                    for a in &active_workers {
                        let id8 = trunc(&a.id, 8);
                        let type_label = a.assignment_type.as_deref().unwrap_or("work");
                        let text = StyledText {
                            spans: vec![
                                StyledSpan::with_fg(
                                    format!("  {} ", id8),
                                    Color::rgb(100, 100, 180),
                                ),
                                StyledSpan::with_fg(
                                    format!("#{:<6}", a.issue_number),
                                    Color::rgb(150, 150, 240),
                                ),
                                StyledSpan::with_fg(
                                    format!("{:<8}", type_label),
                                    Color::rgb(150, 180, 150),
                                ),
                                StyledSpan::with_fg(
                                    format!("  {}", trunc(&a.repo, 18)),
                                    Color::rgb(110, 110, 110),
                                ),
                            ],
                        };
                        items.push(ListItem {
                            text,
                            icon: None,
                            detail: Some(StyledText {
                                spans: vec![StyledSpan::with_fg(
                                    a.age_str(),
                                    Color::rgb(90, 90, 90),
                                )],
                            }),
                            decoration: Decoration::Normal,
                        });
                    }
                }

                items.push(kv_item("", "", None)); // blank

                // ── Job history ──────────────────────────────────────────
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
                        let sc = a.status_color(&self.active_theme);
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

                // ── Action hints ─────────────────────────────────────────
                if m.reachable {
                    items.push(kv_item("", "", None));
                    items.push(kv_item(
                        "",
                        "  r=restart  u=update  c=clean worktrees",
                        Some(Color::rgb(140, 140, 100)),
                    ));
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
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    // ── #207: Machine metrics sparklines ─────────────────────────────────

    /// Render CPU and memory sparklines for the currently-selected machine
    /// into `area`.  The area is split vertically: top half = CPU sparkline
    /// with a label row, bottom half = memory sparkline with a label row.
    ///
    /// When no samples have been collected yet (e.g. the machine is
    /// unreachable or the user just switched to the Machines panel), a
    /// subdued placeholder is drawn instead so the layout stays stable.
    fn render_machine_sparklines(&self, backend: &mut dyn Backend, area: Rect, lh: f32) {
        if area.height < lh * 2.0 {
            return; // Not enough vertical room to be useful.
        }

        let machine_name = match self.data.machines.get(self.machine_sel) {
            Some(m) => &m.name,
            None => return,
        };

        let samples: &[MetricSample] = self
            .machine_metrics
            .get(machine_name)
            .map(|d| d.as_slices().0) // front slice; fine for display
            .unwrap_or(&[]);

        // Split area into top (CPU) and bottom (Mem) halves.
        let half_h = area.height / 2.0;
        let cpu_area = Rect::new(area.x, area.y, area.width, half_h);
        let mem_area = Rect::new(area.x, area.y + half_h, area.width, half_h);

        // Render CPU row.
        let cpu_data: Vec<f64> = samples.iter().map(|s| s.cpu as f64).collect();
        let last_cpu = samples.last().map(|s| s.cpu).unwrap_or(0.0);
        self.render_metric_sparkline(
            backend, cpu_area, lh,
            "CPU",
            last_cpu,
            cpu_data,
            Color::rgb(80, 160, 240),
        );

        // Render memory row.
        let mem_data: Vec<f64> = samples.iter().map(|s| s.mem as f64).collect();
        let last_mem = samples.last().map(|s| s.mem).unwrap_or(0.0);
        self.render_metric_sparkline(
            backend, mem_area, lh,
            "Mem",
            last_mem,
            mem_data,
            Color::rgb(120, 200, 120),
        );
    }

    /// Draw a single labelled sparkline row.
    ///
    /// Layout: one `lh`-tall label+value row on top, the rest of the
    /// rect for the sparkline chart body.  When `data` is empty a
    /// placeholder "—" value is shown and no chart is drawn.
    fn render_metric_sparkline(
        &self,
        backend: &mut dyn Backend,
        area: Rect,
        lh: f32,
        label: &str,
        last_val: f32,
        data: Vec<f64>,
        color: Color,
    ) {
        if area.height < lh {
            return;
        }
        let label_h = lh;
        let chart_h = (area.height - label_h).max(0.0);
        let label_rect = Rect::new(area.x, area.y, area.width, label_h);
        let chart_rect = Rect::new(area.x, area.y + label_h, area.width, chart_h);

        // Label row — "CPU  42%" or "Mem  67%" with subdued colouring.
        let val_str = if data.is_empty() {
            "  —".to_string()
        } else {
            format!("  {:.0}%", last_val)
        };
        backend.draw_list(label_rect, &ListView {
            id: WidgetId::new(format!("metric-label-{}", label.to_lowercase())),
            title: None,
            items: vec![ListItem {
                text: StyledText {
                    spans: vec![
                        StyledSpan::with_fg(
                            format!(" {} ", label),
                            Color::rgb(120, 120, 140),
                        ),
                        StyledSpan::with_fg(val_str, color),
                    ],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Normal,
            }],
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: false,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        });

        // Sparkline body — only when we have data and enough vertical room.
        if data.is_empty() || chart_h < 1.0 {
            return;
        }
        let chart = Chart {
            id: WidgetId::new(format!("metric-chart-{}", label.to_lowercase())),
            kind: ChartKind::Sparkline,
            series: vec![Series {
                label: label.to_string(),
                data,
                color: Some(color),
                fill: false,
            }],
            x_label: None,
            y_label: None,
            y_range: Some((0.0, 100.0)),
            x_range: None,
            show_legend: false,
            y_ticks: None,
            x_ticks: None,
            show_grid: false,
        };
        backend.draw_chart(chart_rect, &chart, None, None);
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
            // #738: "merge" is retired from per-issue pipeline; it lives
            // solely in the Merge Queue panel (Phase 3, SidebarView::MergeQueue).
            if g != "work" && g != "plan" && g != "merge" {
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
        // An open issue whose merge_queue entry is "merged" is logically
        // completed — the PR closed the issue via `fixes #N` even before the
        // brain has synced the GitHub close.  Mirror the Board classifier
        // (IssueGroup::lifecycle_section) which already treats merged as done.
        if self.merge_stage_status_for(issue) == StageStatus::Done {
            return "done";
        }
        // Only *workable* assignments make an issue in-progress.  Scoping
        // conversations — `refinement`, `new-issue-chat`, `test-chat`, and the
        // #628 "Chat about issue" (`chat`) — are NOT pipeline execution, so a
        // chat-/refinement-only issue belongs in New, not Active.  Use the same
        // `is_workable_type` predicate as the Board classifier; the old inline
        // list omitted `chat`, which pinned chat-only issues (e.g. #258) to
        // In-progress:Idle and made "Drop to backlog" appear to do nothing.
        let has_work_assignment = self.data.assignments.iter().any(|a| {
            a.issue_number == issue.number
                && issue
                    .coord_repo
                    .as_deref()
                    .map(|r| r == a.repo)
                    .unwrap_or(true)
                && a.assignment_type
                    .as_deref()
                    .map(is_workable_type)
                    .unwrap_or(true)
        });
        if has_work_assignment {
            return "in-progress";
        }
        // #628: a coord-tracked, not-yet-started issue is just "new". Neither
        // `status:ready` (Pending) nor `status:refining` (Refining) splits a
        // separate bucket anymore — they gate no dispatch, so the New/Pending and
        // Refining distinctions were display-only. The empty pending/refining
        // sections auto-hide; the Pipeline is New → In-progress → Done.
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

    /// #546: Sum ``cost_usd`` across ALL assignments for an issue (rollup).
    ///
    /// Returns ``None`` when no assignment has a captured cost yet (e.g. all
    /// workers are still running or all rows predate #208).  Interactive / Max
    /// subscription sessions have ``cost_usd = NULL`` and are excluded from
    /// the sum (they are shown as "Max" in the per-assignment rows instead).
    ///
    /// The rollup shows the coordinator the TOTAL spend for an issue across all
    /// stage iterations: plan + work + fix-1 + fix-2 + review + smoke.
    fn issue_total_cost(&self, issue: &PipelineIssue) -> Option<f64> {
        let local_repo = issue.coord_repo.as_deref();
        let costs: Vec<f64> = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == *r,
                None => true,
            })
            .filter_map(|a| a.cost_usd)
            .filter(|&c| c > 0.0)
            .collect();
        if costs.is_empty() {
            None
        } else {
            Some(costs.iter().sum())
        }
    }

    /// #546: Total token count (input + output) for an issue across all
    /// assignments.  Returns 0 when no tokens have been persisted yet.
    fn issue_total_tokens(&self, issue: &PipelineIssue) -> i64 {
        let local_repo = issue.coord_repo.as_deref();
        self.data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == *r,
                None => true,
            })
            .map(|a| a.input_tokens + a.output_tokens)
            .sum()
    }

    /// Return the coord_repo name for an issue, falling back to the repo_slug.
    fn pipeline_repo_key(issue: &PipelineIssue) -> &str {
        issue.coord_repo.as_deref().unwrap_or(&issue.repo_slug)
    }

    /// Group pipeline issues under a repo into non-empty lifecycle sections.
    ///
    /// #194: the Pipeline panel displays all **five lifecycle sections** —
    /// **New** (no `status:*` label), **Refining** (`status:refining`),
    /// **Pending** (`status:ready`, no work assignments), **In-progress**
    /// (active work assignment), and **Done** (closed / merged).  The
    /// classifier keys returned by `pipeline_lifecycle_section` map 1:1
    /// to the display labels — the Pipeline sidebar surfaces the full
    /// lifecycle so the TUI can drive every stage.
    ///
    /// Returns `(lifecycle_key, Vec<pipeline_issues index>)` in display
    /// order (New → Refining → Pending → In-progress → Done), skipping
    /// empty sections.  Issues that the user has dismissed via 'D' are
    /// excluded.
    ///
    /// Note: used in tests to verify per-repo lifecycle grouping; the
    /// production sidebar uses `pipeline_active_issues` and
    /// `pipeline_repos_for_state` directly.
    #[cfg(test)]
    fn pipeline_groups_for_repo(&self, repo_key: &str) -> Vec<(&'static str, Vec<usize>)> {
        // #194: full five-section lifecycle — New / Refining / Pending /
        // In-progress / Done — surfaced in display order.
        const VISIBLE_LIFECYCLE: [&str; 5] =
            ["new", "refining", "pending", "in-progress", "done"];

        // #290: deduplicate by (repo_slug, issue_number) — keep the *last*
        // occurrence so that a newer closed entry (from the gh --state=closed
        // query) beats a stale open entry (from --state=open) when the two
        // queries race around an issue that just closed.  Without this, the
        // same issue can appear in both "in-progress" (open copy) and "done"
        // (closed copy) simultaneously during the transition window.
        let mut dedup: std::collections::HashMap<(String, u64), usize> =
            std::collections::HashMap::new();
        for (i, issue) in self.pipeline_issues.iter().enumerate() {
            if Self::pipeline_repo_key(issue) == repo_key
                && !self
                    .pipeline_dismissed
                    .contains(&(issue.repo_slug.clone(), issue.number))
            {
                // Last write wins — later index = more recent entry from fetch.
                dedup.insert((issue.repo_slug.clone(), issue.number), i);
            }
        }

        let mut result: Vec<(&'static str, Vec<usize>)> = Vec::new();
        for &lc in &VISIBLE_LIFECYCLE {
            let mut idxs: Vec<usize> = dedup
                .values()
                .copied()
                .filter(|&i| {
                    let issue = &self.pipeline_issues[i];
                    self.pipeline_lifecycle_section(issue) == lc
                        // FILTER box: drop non-matching issues so empty
                        // lifecycle groups (and, in the rebuild, empty repo
                        // sections) fall away — matching Board behavior.
                        && self.pipeline_search.matches(issue.number, &issue.title)
                })
                .collect();
            // Sort to restore stable ordering by original position in pipeline_issues.
            idxs.sort_unstable();
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

    /// Compute a short uppercase repo tag for display in the Active section.
    ///
    /// If `repo_name`'s first character is unique (case-insensitive) among
    /// `all_repos`, returns that single character uppercased.  On collision,
    /// extends to the shortest prefix of `repo_name` that is not shared by
    /// any other entry in `all_repos`; the first character of the prefix is
    /// uppercased and the rest are kept lowercase.
    ///
    /// Examples:
    /// - `["quadraui"]` → `"Q"`
    /// - `["claude-coordinator", "coord-something"]` → `"Cl"` / `"Co"`
    fn repo_tag(repo_name: &str, all_repos: &[String]) -> String {
        let Some(first_char) = repo_name.chars().next() else {
            return "?".to_string();
        };
        let first_lower = first_char.to_ascii_lowercase();

        // Check if any OTHER repo starts with the same letter.
        let has_conflict = all_repos.iter().any(|r| {
            r.as_str() != repo_name
                && r.chars().next().map(|c| c.to_ascii_lowercase()) == Some(first_lower)
        });

        if !has_conflict {
            // No collision — single uppercase char.
            return first_char.to_uppercase().collect();
        }

        // Find the shortest prefix of repo_name that is unique across all_repos.
        for len in 2..=repo_name.len() {
            if !repo_name.is_char_boundary(len) {
                continue;
            }
            let prefix_lower = repo_name[..len].to_lowercase();
            let collision = all_repos
                .iter()
                .any(|r| r.as_str() != repo_name && r.to_lowercase().starts_with(&prefix_lower));
            if !collision {
                let mut chars = repo_name[..len].chars();
                let tag: String = chars
                    .next()
                    .map(|c| {
                        let upper: String = c.to_uppercase().collect();
                        upper
                    })
                    .unwrap_or_default()
                    + chars.as_str();
                return tag;
            }
        }

        // Fallback: full name with first char uppercased.
        let mut chars = repo_name.chars();
        chars
            .next()
            .map(|c| {
                let upper: String = c.to_uppercase().collect();
                upper + chars.as_str()
            })
            .unwrap_or_default()
    }

    /// Return a sorted list of `pipeline_issues` indices for all in-progress
    /// issues across all repos.  Applies dedup (last-write-wins by
    /// `(repo_slug, issue_number)`), search filter, and dismissal filter.
    fn pipeline_active_issues(&self) -> Vec<usize> {
        let mut dedup: std::collections::HashMap<(String, u64), usize> =
            std::collections::HashMap::new();
        for (i, issue) in self.pipeline_issues.iter().enumerate() {
            if !self
                .pipeline_dismissed
                .contains(&(issue.repo_slug.clone(), issue.number))
            {
                dedup.insert((issue.repo_slug.clone(), issue.number), i);
            }
        }
        let mut idxs: Vec<usize> = dedup
            .values()
            .copied()
            .filter(|&i| {
                let issue = &self.pipeline_issues[i];
                self.pipeline_lifecycle_section(issue) == "in-progress"
                    && self.pipeline_search.matches(issue.number, &issue.title)
            })
            .collect();
        idxs.sort_unstable();
        idxs
    }

    /// Whether a live `coord-<id>` tmux session currently exists for `issue`
    /// (local OR remote, via `live_tmux_sessions`).  Matched precisely by
    /// coord-local repo + issue number so a same-number session in another
    /// repo doesn't false-positive (#480).  A session counts as live whether
    /// or not a human is attached — it's running in tmux either way.
    fn issue_session_is_live(&self, issue: &PipelineIssue) -> bool {
        let repo = Self::pipeline_repo_key(issue);
        self.live_tmux_sessions.iter().any(|s| {
            s.issue_number == Some(issue.number) && s.repo_name.as_deref() == Some(repo)
        })
    }

    /// Split the in-progress ("Active") issues into two ordered groups by
    /// whether a live claude session exists: `"live"` then `"idle"`.  Only
    /// non-empty groups are returned — mirroring `pipeline_repos_for_state`'s
    /// `(group_key, issue_indices)` shape so the nested `[group, child]` path
    /// handling is identical for every Pipeline section.
    fn pipeline_active_by_liveness(&self) -> Vec<(String, Vec<usize>)> {
        let mut live: Vec<usize> = Vec::new();
        let mut idle: Vec<usize> = Vec::new();
        for idx in self.pipeline_active_issues() {
            if self.issue_session_is_live(&self.pipeline_issues[idx]) {
                live.push(idx);
            } else {
                idle.push(idx);
            }
        }
        let mut groups: Vec<(String, Vec<usize>)> = Vec::new();
        if !live.is_empty() {
            groups.push(("live".to_string(), live));
        }
        if !idle.is_empty() {
            groups.push(("idle".to_string(), idle));
        }
        groups
    }

    /// Display label for an Active liveness group key (`"live"` → "Live").
    fn liveness_group_label(key: &str) -> &'static str {
        match key {
            "live" => "Live",
            _ => "Idle",
        }
    }

    // ── #728: Done-section windowing helpers ─────────────────────────────────

    /// Compute the "done-at" timestamp for an issue: the **max** `finished_at`
    /// across all assignments for that issue (by issue number + coord-repo).
    ///
    /// Returns `None` when no assignment has a `finished_at` (e.g. the issue
    /// was closed on GitHub with no coord pipeline rows, or rows predate the
    /// column).  Such issues are excluded from the windowed view and only
    /// appear when `done_window == All`.
    fn issue_done_at(&self, issue: &PipelineIssue) -> Option<f64> {
        let local_repo = issue.coord_repo.as_deref();
        self.data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == *r,
                None => true,
            })
            .filter_map(|a| a.finished_at)
            .reduce(f64::max)
    }

    /// Return a flat, deduplicated, time-windowed, newest-first list of
    /// `pipeline_issues` indices for the **Done** lifecycle section.
    ///
    /// - Applies dedup (last-write-wins), search filter, and dismissal filter.
    /// - Issues inside `self.done_window` are included; older ones are excluded
    ///   unless `done_window == All`.
    /// - Issues with `None` done-at are treated as "old" and only appear in `All`.
    /// - Sorted newest-first; `None` timestamps go last.
    fn pipeline_done_windowed(&self) -> Vec<usize> {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64();

        // Dedup: last write wins (same semantics as pipeline_repos_for_state).
        let mut dedup: std::collections::HashMap<(String, u64), usize> =
            std::collections::HashMap::new();
        for (i, issue) in self.pipeline_issues.iter().enumerate() {
            if !self
                .pipeline_dismissed
                .contains(&(issue.repo_slug.clone(), issue.number))
                && self.pipeline_search.matches(issue.number, &issue.title)
            {
                dedup.insert((issue.repo_slug.clone(), issue.number), i);
            }
        }

        let window_secs = self.done_window.secs();

        // Collect done issues with their timestamps, filtered by window.
        let mut entries: Vec<(usize, Option<f64>)> = dedup
            .values()
            .copied()
            .filter(|&i| {
                self.pipeline_lifecycle_section(&self.pipeline_issues[i]) == "done"
            })
            .map(|i| {
                let done_at = self.issue_done_at(&self.pipeline_issues[i]);
                (i, done_at)
            })
            .filter(|(_, done_at)| match (done_at, window_secs) {
                (_, None) => true,               // All: include everything
                (Some(t), Some(w)) => now - t <= w, // within window
                (None, Some(_)) => false,         // unknown time: hide in windowed view
            })
            .collect();

        // Sort: known timestamps newest-first, unknowns at the bottom.
        entries.sort_by(|(_, a), (_, b)| match (a, b) {
            (Some(at), Some(bt)) => bt.partial_cmp(at).unwrap_or(std::cmp::Ordering::Equal),
            (Some(_), None) => std::cmp::Ordering::Less,
            (None, Some(_)) => std::cmp::Ordering::Greater,
            (None, None) => std::cmp::Ordering::Equal,
        });

        entries.into_iter().map(|(i, _)| i).collect()
    }

    /// True when the Pipeline Done section is the active sidebar section.
    /// Used to gate the `→` extend-range key.
    fn is_done_section_active(&self) -> bool {
        let search_offset = 1usize;
        if let Some(section) = self.pipeline_sidebar.active_section() {
            if section >= search_offset {
                let state_idx = section - search_offset;
                if let Some(&key) = self.pipeline_state_section_names.get(state_idx) {
                    return key == "done";
                }
            }
        }
        false
    }

    /// Return issues for a lifecycle state, grouped by repo, in stable repo
    /// order (same order as `pipeline_repo_names`).  Only intended for
    /// `"new"`, `"refining"`, `"pending"`, and `"done"` — Active issues are
    /// handled by `pipeline_active_issues` / `pipeline_active_by_liveness` instead.
    ///
    /// Applies dedup, search filter, and dismissal filter.  Empty repos are
    /// omitted.
    fn pipeline_repos_for_state(&self, lc_key: &'static str) -> Vec<(String, Vec<usize>)> {
        let mut dedup: std::collections::HashMap<(String, u64), usize> =
            std::collections::HashMap::new();
        for (i, issue) in self.pipeline_issues.iter().enumerate() {
            if !self
                .pipeline_dismissed
                .contains(&(issue.repo_slug.clone(), issue.number))
                && self.pipeline_search.matches(issue.number, &issue.title)
            {
                dedup.insert((issue.repo_slug.clone(), issue.number), i);
            }
        }
        // Build repo order from pipeline_issues (stable insertion order).
        let mut repos: Vec<String> = Vec::new();
        for issue in &self.pipeline_issues {
            let key = Self::pipeline_repo_key(issue).to_string();
            if !repos.contains(&key) {
                repos.push(key);
            }
        }
        let mut result: Vec<(String, Vec<usize>)> = Vec::new();
        for repo_key in repos {
            let mut idxs: Vec<usize> = dedup
                .values()
                .copied()
                .filter(|&i| {
                    let issue = &self.pipeline_issues[i];
                    Self::pipeline_repo_key(issue) == repo_key.as_str()
                        && self.pipeline_lifecycle_section(issue) == lc_key
                })
                .collect();
            idxs.sort_unstable();
            if !idxs.is_empty() {
                result.push((repo_key, idxs));
            }
        }
        result
    }

    /// #668: Group a slice of `pipeline_issues` indices by milestone.
    ///
    /// Looks up milestone data from `self.data.open_issues` (matched by
    /// `coord_repo` + `number`).  Issues without an `open_issues` record, or
    /// whose record has no milestone, fall into the `"no-milestone"` bucket.
    ///
    /// Returns `Vec<(milestone_key, display_title, Vec<usize>)>` sorted:
    /// named milestones by number ASC, `"No milestone"` last.
    fn pipeline_milestones_for_issues(
        &self,
        issue_idxs: &[usize],
    ) -> Vec<(String, String, Vec<usize>)> {
        let mut milestone_map: std::collections::BTreeMap<
            (i64, String),
            (String, String, Vec<usize>),
        > = std::collections::BTreeMap::new();

        for &idx in issue_idxs {
            let issue = &self.pipeline_issues[idx];
            // Lookup milestone from open_issues by (coord_repo, number).
            let (mil_num, mil_title) = issue
                .coord_repo
                .as_ref()
                .and_then(|repo_name| {
                    self.data
                        .open_issues
                        .iter()
                        .find(|oi| oi.repo_name == *repo_name && oi.number == issue.number)
                })
                .map(|oi| (oi.milestone_number, oi.milestone_title.clone()))
                .unwrap_or((None, None));

            match mil_num {
                Some(n) => {
                    let title = mil_title.unwrap_or_default();
                    let key = n.to_string();
                    let sort_key = (n, title.clone());
                    milestone_map
                        .entry(sort_key)
                        .or_insert_with(|| (key, title, Vec::new()))
                        .2
                        .push(idx);
                }
                None => {
                    let sort_key = (i64::MAX, String::new());
                    milestone_map
                        .entry(sort_key)
                        .or_insert_with(|| {
                            (
                                "no-milestone".to_string(),
                                "No milestone".to_string(),
                                Vec::new(),
                            )
                        })
                        .2
                        .push(idx);
                }
            }
        }

        milestone_map
            .into_values()
            .filter(|(_, _, idxs)| !idxs.is_empty())
            .collect()
    }

    /// Returns a map from GitHub repo slug (`owner/name`) to the count of
    /// `state='pending'` rows in the in-memory merge-queue snapshot.
    ///
    /// Used by `rebuild_pipeline_sidebar` to badge repo sub-header rows with
    /// a queue-depth indicator (amber when the depth exceeds 5).  Pure read of
    /// `self.data.merge_queue` — no network I/O, safe to call on every render.
    fn pending_merge_queue_depth_by_slug(&self) -> std::collections::HashMap<String, usize> {
        let mut map: std::collections::HashMap<String, usize> =
            std::collections::HashMap::new();
        for e in &self.data.merge_queue {
            if e.state == "pending" {
                *map.entry(e.repo_github.clone()).or_insert(0) += 1;
            }
        }
        map
    }

    // ── #737: Merge Queue panel helpers ──────────────────────────────────────

    /// `true` when any merge-queue entry requires human attention:
    /// NEEDS_ATTENTION plan-status (from `merge_plan`), or — when only the
    /// legacy `merge_queue` is available — HUMAN_REQUIRED/failed state or
    /// a known CI failure.
    fn merge_queue_needs_attention(&self) -> bool {
        if !self.data.merge_plan.is_empty() {
            // #776 plan path: surface "NEEDS_ATTENTION" status from the server.
            return self
                .data
                .merge_plan
                .iter()
                .any(|p| p.status == "NEEDS_ATTENTION");
        }
        // Legacy fallback: derive attention from raw merge_queue state.
        self.data.merge_queue.iter().any(|e| {
            matches!(e.state.as_str(), "human_required" | "failed")
                || (e.state == "pending"
                    && e.pr_number.is_some()
                    && self.ci_failed_for_entry(e))
        })
    }

    /// Return a reference to the currently-selected merge-queue entry, if any.
    fn selected_merge_queue_entry(&self) -> Option<&MergeQueueEntry> {
        let n = self.data.merge_queue.len();
        if n == 0 {
            return None;
        }
        self.data.merge_queue.get(self.merge_queue_sel.min(n - 1))
    }

    /// Return a reference to the currently-selected planned merge entry, if any.
    ///
    /// Used when `data.merge_plan` is non-empty (v0.4.53+ daemon path).
    /// `merge_queue_sel` indexes into the rendered plan list order.
    fn selected_merge_plan_entry(&self) -> Option<&PlannedMergeEntry> {
        let n = self.data.merge_plan.len();
        if n == 0 {
            return None;
        }
        self.data.merge_plan.get(self.merge_queue_sel.min(n - 1))
    }

    /// Format a single merge-queue entry as a terse list label.
    ///
    /// Template: `[<STATE>] #<PR>  <issue_title>  (<error-reason>)`
    fn merge_queue_entry_label(&self, entry: &MergeQueueEntry) -> String {
        // State badge.
        let state = match entry.state.as_str() {
            "pending" => "PENDING",
            "active" | "running" => "ACTIVE",
            "merged" => "MERGED",
            "human_required" => "NEEDS ATTENTION",
            "failed" => "FAILED",
            _ => &entry.state,
        };
        // PR number.
        let pr = entry
            .pr_number
            .map(|n| format!(" #{}", n))
            .unwrap_or_default();
        // Issue title — join from open_issues on (repo_name, issue_number).
        let title = entry
            .issue_number
            .and_then(|num| {
                // Reverse-lookup: repo_github → coord repo name, then match open_issues.
                let coord_repo = self
                    .data
                    .pipeline_repos
                    .iter()
                    .find(|(_, gh)| gh == &entry.repo_github)
                    .map(|(name, _)| name.as_str());
                self.data.open_issues.iter().find(|oi| {
                    oi.number == num
                        && coord_repo.is_some_and(|cr| oi.repo_name == cr)
                })
            })
            .map(|oi| format!("  {}", oi.title))
            .unwrap_or_default();
        // Gate / conflict reason — the last error string from coord merge.
        let reason = entry
            .error
            .as_deref()
            .filter(|e| !e.is_empty())
            .map(|e| {
                // Truncate long error strings to keep the row terse.
                let s = e.trim();
                if s.len() > 60 {
                    format!("  ({}…)", &s[..57])
                } else {
                    format!("  ({})", s)
                }
            })
            .unwrap_or_default();
        format!("[{}]{}{}{}", state, pr, title, reason)
    }

    /// State colour for a merge-queue entry label.
    fn merge_queue_entry_color(&self, entry: &MergeQueueEntry) -> Color {
        match entry.state.as_str() {
            "human_required" | "failed" => Color::rgb(220, 70, 70),
            "merged" => Color::rgb(80, 200, 80),
            "active" | "running" => Color::rgb(80, 160, 220),
            _ => {
                // Pending: amber when CI is failing.
                if self.ci_failed_for_entry(entry) {
                    Color::rgb(220, 140, 40)
                } else {
                    Color::rgb(180, 180, 180)
                }
            }
        }
    }

    /// Sidebar placeholder for the Merge Queue view — shows entry count and
    /// an attention indicator when any entry needs human action.
    fn merge_queue_sidebar(&self) -> ListView {
        // Use merge_plan count when available (#776); fall back to merge_queue.
        let n = if !self.data.merge_plan.is_empty() {
            self.data.merge_plan.len()
        } else {
            self.data.merge_queue.len()
        };
        let attn = if self.merge_queue_needs_attention() {
            " ⚠ needs attention"
        } else {
            ""
        };
        let hint = format!("  {} entr{}{}",
            n,
            if n == 1 { "y" } else { "ies" },
            attn,
        );
        ListView {
            id: WidgetId::new("mergequeue-sidebar"),
            title: Some(StyledText::plain(" MERGE QUEUE ")),
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

    /// Render the Merge Queue main panel.
    ///
    /// **#777 — plan path (v0.4.53+ daemon):** when `data.merge_plan` is
    /// non-empty, renders the server-computed ranked plan grouped by
    /// `repo → target_branch`.  Each entry shows its rank number, plan
    /// status badge, PR number, diff size, issue title, live gate reason
    /// (for BLOCKED), and age.  The order matches what `coord merge` would do.
    ///
    /// **Legacy fallback:** when `merge_plan` is empty (older daemon or local
    /// SQLite path), falls back to displaying `data.merge_queue` entries
    /// grouped by milestone as in #737.
    fn render_merge_queue_panel(&self, backend: &mut dyn Backend, rect: Rect, _lh: f32) {
        // ── #777: plan path ───────────────────────────────────────────────────
        if !self.data.merge_plan.is_empty() {
            self.render_merge_plan_panel(backend, rect);
            return;
        }

        // ── Legacy path (no merge_plan from daemon) ───────────────────────────
        let n = self.data.merge_queue.len();
        let staging = &self.data.merge_staging;

        // Empty-state: no staging AND no queue entries.
        if n == 0 && staging.is_empty() {
            backend.draw_list(rect, &plain_list(
                "mergequeue-empty",
                "  No merge-queue entries — run `coord merge` to populate the queue.",
                0,
            ));
            return;
        }

        // Clamp the selection in case the queue shrank after a drop.
        let sel_entry = if n > 0 { self.merge_queue_sel.min(n - 1) } else { 0 };

        // ── Build item list ───────────────────────────────────────────────
        //
        // Layout (top → bottom):
        //   [staging header]            — only when staging is non-empty
        //   [staging rows]
        //   [queue header]              — only when BOTH sections are non-empty
        //   [milestone sub-headers]
        //   [queue rows]
        //
        // The staging section answers "did my approved PR make it into the
        // queue?" and is always shown above the active queue so it's the
        // first thing seen when opening the panel.
        //
        // `selected_idx` tracks where the selected queue entry lands in the
        // combined list, accounting for all inserted header rows above it.

        let mut items: Vec<ListItem> = Vec::with_capacity(staging.len() + n + 10);
        let mut selected_idx = 0usize;

        // ── #778: Staging section ──────────────────────────────────────────
        if !staging.is_empty() {
            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(
                        " About to enter queue ".to_string(),
                        Color::rgb(180, 210, 150),
                    )],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Header,
            });

            for entry in staging {
                let issue_part = format!("#{:<5} {}", entry.issue_number, trunc(&entry.issue_title, 22));
                let (arrow_text, arrow_color) = if entry.status == "blocked" {
                    let reason = entry.reason.as_deref().unwrap_or("gate failing");
                    (format!("→  BLOCKED: {}", reason), Color::rgb(220, 90, 80))
                } else {
                    ("→  enqueuing on next tick".to_string(), Color::rgb(130, 200, 120))
                };
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![
                            StyledSpan::with_fg(issue_part, Color::rgb(150, 150, 240)),
                            StyledSpan::with_fg("  ".to_string(), Color::rgb(120, 120, 120)),
                            StyledSpan::with_fg(arrow_text, arrow_color),
                        ],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Normal,
                });
            }
        }

        // ── Merge queue section ────────────────────────────────────────────
        if n > 0 {
            // When both sections are visible, add a separator so the reader
            // can immediately distinguish staged-but-pending from in-queue.
            if !staging.is_empty() {
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(
                            " In merge queue ".to_string(),
                            Color::rgb(150, 190, 230),
                        )],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                });
            }

            // Milestone-grouped queue entries (existing behaviour).
            let mut last_ms: Option<Option<String>> = None; // sentinel: never matches first
            for (i, entry) in self.data.merge_queue.iter().enumerate() {
                let ms = entry.milestone_title.clone();
                if last_ms.as_ref() != Some(&ms) {
                    let header_label = ms.as_deref().unwrap_or("(No milestone)");
                    items.push(ListItem {
                        text: StyledText {
                            spans: vec![StyledSpan::with_fg(
                                format!(" {} ", header_label),
                                Color::rgb(150, 190, 230),
                            )],
                        },
                        icon: None,
                        detail: None,
                        decoration: Decoration::Header,
                    });
                    last_ms = Some(ms);
                }
                if i == sel_entry {
                    selected_idx = items.len();
                }
                let label = self.merge_queue_entry_label(entry);
                let color = self.merge_queue_entry_color(entry);
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(&label, color)],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Normal,
                });
            }
        }

        let total = items.len();
        backend.draw_list(rect, &ListView {
            id: WidgetId::new("mergequeue-list"),
            title: Some(StyledText::plain(" MERGE QUEUE ")),
            items,
            selected_idx,
            scroll_offset: self.merge_queue_scroll,
            has_focus: true,
            bordered: true,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: total > 10,
        });
    }

    // ── #777: Ranked plan panel ───────────────────────────────────────────────

    /// Render the Merge Queue panel from `data.merge_plan` (#776 plan path).
    ///
    /// Layout (per the #777 spec):
    /// ```text
    /// MERGE QUEUE — order = next coord merge run
    ///
    /// repo-name → target-branch
    ///   1. [READY]   #812  +14   Fix toast clipping        queued 4m ago
    ///   2. [BLOCKED] #770  +120  CI running (2 checks)      last try 2m ago
    /// other-repo → develop
    ///   3. [READY]   #383  +41   ButtonBar primitive        queued 1h ago
    /// ```
    ///
    /// Entries are grouped by `(repo_github, target_branch)` via
    /// `Decoration::Header` separators.  Group headers show
    /// `<repo_name> → <target_branch>` using the coord-local repo name from
    /// `pipeline_repos` (falling back to the GitHub slug when not mapped).
    /// Each entry row includes rank, status badge, PR number (from
    /// `merge_queue` by `assignment_id`), size, truncated title, and age.
    fn render_merge_plan_panel(&self, backend: &mut dyn Backend, rect: Rect) {
        // Called only when merge_plan is non-empty (see render_merge_queue_panel).
        let plan = &self.data.merge_plan;
        let n = plan.len();

        // Build a lookup: assignment_id → pr_number (from the raw merge_queue).
        let pr_lookup: std::collections::HashMap<&str, i64> = self
            .data
            .merge_queue
            .iter()
            .filter_map(|mq| mq.pr_number.map(|pr| (mq.assignment_id.as_str(), pr)))
            .collect();

        // Current time for age formatting.  Using a 0 fallback (shows "" for
        // missing timestamps) is safe here — the field is cosmetic only.
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);

        let sel_entry = self.merge_queue_sel.min(n - 1);
        let mut items: Vec<ListItem> = Vec::with_capacity(n + 8);
        let mut selected_idx = 0usize;
        // Consecutive-run grouping by (repo_github, target_branch).
        let mut last_group: Option<(&str, &str)> = None;

        for (i, entry) in plan.iter().enumerate() {
            // ── Group header ─────────────────────────────────────────────
            let group_key = (entry.repo_github.as_str(), entry.target_branch.as_str());
            if last_group != Some(group_key) {
                // `repo_name` from the plan is the coord-local name; prefer it
                // over the GitHub slug for a friendlier group header.
                let display_repo = if !entry.repo_name.is_empty() {
                    &entry.repo_name
                } else {
                    &entry.repo_github
                };
                let header_label = format!(" {} → {} ", display_repo, entry.target_branch);
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(header_label, Color::rgb(150, 190, 230))],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                });
                last_group = Some(group_key);
            }

            // ── Record selection display index ────────────────────────────
            if i == sel_entry {
                selected_idx = items.len();
            }

            // ── Format the entry label ────────────────────────────────────
            // Status badge: map plan status to display string.
            let status_badge = match entry.status.as_str() {
                "READY"           => "READY",
                "BLOCKED"         => "BLOCKED",
                "MERGING"         => "MERGING",
                "MERGED"          => "MERGED",
                "NEEDS_ATTENTION" => "NEEDS ATTENTION",
                other             => other,
            };

            // PR number (cross-ref with raw merge_queue by assignment_id).
            let pr_str = pr_lookup
                .get(entry.assignment_id.as_str())
                .map(|&pr| format!(" #{}", pr))
                .unwrap_or_default();

            // Diff size.
            let size_str = entry.size
                .map(|s| format!(" +{}", s))
                .unwrap_or_default();

            // Title — truncate to keep the row terse.
            let title_short = trunc(&entry.issue_title, 40);

            // Age: show `enqueued_at` for READY/BLOCKED, `last_attempt` for
            // MERGING/terminal states that have a recent attempt.
            let age_str = if matches!(entry.status.as_str(), "MERGED" | "NEEDS_ATTENTION") {
                // Terminal / attention: show last_attempt if any.
                let age = format_age(entry.last_attempt, now);
                if age.is_empty() { String::new() } else { format!("  last try {}", age) }
            } else {
                // Active/blocked: prefer last_attempt for MERGING, enqueued_at otherwise.
                let ts = if entry.status == "MERGING" {
                    entry.last_attempt.or(entry.enqueued_at)
                } else {
                    entry.enqueued_at
                };
                // Only use "last try" when the entry is actively MERGING;
                // for READY/BLOCKED entries the timestamp is always enqueued_at
                // regardless of whether last_attempt is set, so "queued" is correct.
                let label = if entry.status == "MERGING" { "last try" } else { "queued" };
                let age = format_age(ts, now);
                if age.is_empty() { String::new() } else { format!("  {} {}", label, age) }
            };

            // Reason for BLOCKED entries — the live gate explanation from the plan.
            let reason_str = if entry.status == "BLOCKED" {
                entry.reason
                    .as_deref()
                    .filter(|r| !r.is_empty())
                    .map(|r| {
                        let s = r.trim();
                        if s.len() > 50 {
                            format!("  ({}…)", &s[..47])
                        } else {
                            format!("  ({})", s)
                        }
                    })
                    .unwrap_or_default()
            } else {
                String::new()
            };

            let label = format!(
                "  {}. [{}]{}{} {}{}{}",
                entry.rank, status_badge, pr_str, size_str, title_short, reason_str, age_str,
            );

            // Status colour.
            let color = match entry.status.as_str() {
                "NEEDS_ATTENTION" => Color::rgb(220, 70, 70),
                "MERGED"          => Color::rgb(80, 200, 80),
                "MERGING"         => Color::rgb(80, 160, 220),
                "BLOCKED"         => Color::rgb(220, 140, 40),
                _                 => Color::rgb(180, 180, 180), // READY
            };

            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(label, color)],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Normal,
            });
        }

        let total = items.len();
        backend.draw_list(rect, &ListView {
            id: WidgetId::new("mergequeue-list"),
            title: Some(StyledText::plain(" MERGE QUEUE — order = next coord merge run ")),
            items,
            selected_idx,
            scroll_offset: self.merge_queue_scroll,
            has_focus: true,
            bordered: true,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: total > 10,
        });
    }

    // ── #737 / #780: Merge Queue per-entry actions ───────────────────────────

    /// `coord merge --only <assignment_id>` (single-entry merge, #780).
    ///
    /// Merges exactly the selected entry and leaves all other queue entries
    /// untouched.  When `force` is true, appends `--force-merge` to bypass CI
    /// and gate checks.
    ///
    /// For BLOCKED entries: shows a warning toast with the block reason and
    /// returns without dispatching (the operator can use `M` / force to
    /// override via `--force-merge`).
    fn dispatch_merge_queue_merge_only(&mut self, force: bool) {
        // Prefer merge_plan (v0.4.53+ daemon); fall back to raw merge_queue.
        let (aid, status, reason) = if !self.data.merge_plan.is_empty() {
            match self.selected_merge_plan_entry() {
                Some(e) => (
                    e.assignment_id.clone(),
                    e.status.clone(),
                    e.reason.clone(),
                ),
                None => {
                    self.push_toast("Merge Queue", "No entry selected.", ToastSeverity::Warning);
                    return;
                }
            }
        } else {
            match self.selected_merge_queue_entry() {
                Some(e) => (e.assignment_id.clone(), String::new(), None),
                None => {
                    self.push_toast("Merge Queue", "No entry selected.", ToastSeverity::Warning);
                    return;
                }
            }
        };

        // For BLOCKED entries (without force) surface the reason rather than
        // silently dispatching a merge that will fail the gate.
        if !force && status == "BLOCKED" {
            let body = reason
                .as_deref()
                .filter(|r| !r.is_empty())
                .unwrap_or("gate check failed");
            self.push_toast(
                "Blocked — cannot merge",
                &format!("{}  (use M to force)", body),
                ToastSeverity::Warning,
            );
            return;
        }

        // Build the command as owned Strings so we can conditionally push
        // --force-merge before slicing into &str refs for spawn_queued.
        let mut cmd_strs: Vec<String> = vec![
            "merge".to_string(),
            "--only".to_string(),
            aid.clone(),
        ];
        if force {
            cmd_strs.push("--force-merge".to_string());
        }
        let cmd_refs: Vec<&str> = cmd_strs.iter().map(|s| s.as_str()).collect();
        use crate::commands::SpawnQueuedOutcome;
        match self.command_runner.spawn_queued(&cmd_refs) {
            SpawnQueuedOutcome::Started => {
                self.push_toast(
                    if force { "Force-merge (only) dispatched" } else { "Merge only this dispatched" },
                    &format!("coord merge --only {}{}", aid,
                        if force { " --force-merge" } else { "" }),
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast("⏳ Queued", "merge runs after current command", ToastSeverity::Info);
            }
            SpawnQueuedOutcome::Deduped => {}
        }
    }

    /// `coord merge` — drain every READY entry in the queue after one-key
    /// confirmation.  Sets `pending_merge_all_ready` with the list of READY
    /// aids so the confirm dialog can display the count and a preview.
    fn dispatch_merge_queue_merge_all(&mut self) {
        // Collect all READY aids from merge_plan (preferred) or legacy queue.
        let ready_aids: Vec<String> = if !self.data.merge_plan.is_empty() {
            self.data.merge_plan.iter()
                .filter(|e| e.status == "READY")
                .map(|e| e.assignment_id.clone())
                .collect()
        } else {
            // Legacy merge_queue has no status field; treat all PENDING as ready.
            self.data.merge_queue.iter()
                .map(|e| e.assignment_id.clone())
                .collect()
        };

        if ready_aids.is_empty() {
            self.push_toast(
                "No READY entries",
                "Nothing to merge — all entries are BLOCKED or the queue is empty.",
                ToastSeverity::Info,
            );
            return;
        }

        // Show confirm dialog; key handler fires `coord merge` on 'y'.
        self.pending_merge_all_ready = Some(ready_aids);
    }

    /// `coord merge --drop <assignment_id>` — remove one entry from the queue.
    fn dispatch_merge_queue_drop(&mut self) {
        // Prefer merge_plan (v0.4.53+ daemon); fall back to raw merge_queue.
        let aid = if !self.data.merge_plan.is_empty() {
            match self.selected_merge_plan_entry() {
                Some(e) => e.assignment_id.clone(),
                None => {
                    self.push_toast("Merge Queue", "No entry selected.", ToastSeverity::Warning);
                    return;
                }
            }
        } else {
            match self.selected_merge_queue_entry() {
                Some(e) => e.assignment_id.clone(),
                None => {
                    self.push_toast("Merge Queue", "No entry selected.", ToastSeverity::Warning);
                    return;
                }
            }
        };
        let cmd_strs: Vec<String> = vec![
            "merge".to_string(),
            "--drop".to_string(),
            aid.clone(),
        ];
        let cmd_refs: Vec<&str> = cmd_strs.iter().map(|s| s.as_str()).collect();
        use crate::commands::SpawnQueuedOutcome;
        match self.command_runner.spawn_queued(&cmd_refs) {
            SpawnQueuedOutcome::Started => {
                self.push_toast(
                    "Drop queued",
                    &format!("coord merge --drop {}", aid),
                    ToastSeverity::Info,
                );
                // Advance selection to stay in-bounds after the row disappears.
                self.merge_queue_sel = self.merge_queue_sel.saturating_sub(1);
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast("⏳ Queued", "drop runs after current command", ToastSeverity::Info);
            }
            SpawnQueuedOutcome::Deduped => {}
        }
    }

    // ── #684: Start (automated) > Merge — headless queue drain ───────────────

    /// True when the selected pipeline issue has an active `type="merge"`
    /// assignment (i.e. an interactive `coord assign --merge-of` session is
    /// currently running).  Used to guard the automated merge action so the
    /// headless queue and the interactive agent never race on the same branch.
    fn has_active_interactive_merge_for_issue(&self, issue_num: u64) -> bool {
        self.data.assignments.iter().any(|a| {
            a.issue_number == issue_num
                && a.assignment_type.as_deref() == Some("merge")
                && a.status == "running"
        })
    }

    /// True when the merge queue already has a `state="merging"` entry for
    /// `issue_num` — the headless queue is actively processing that branch.
    /// Used to guard `start-merge-interactive` so an operator doesn't launch
    /// an interactive `--merge-of` agent over a concurrent headless merge.
    fn has_active_headless_merge_for_issue(&self, issue_num: u64) -> bool {
        // Prefer merge_plan (v0.4.53+ daemon); fall back to raw merge_queue.
        if !self.data.merge_plan.is_empty() {
            self.data.merge_plan.iter().any(|e| {
                e.issue_number == issue_num && e.status == "MERGING"
            })
        } else {
            self.data.merge_queue.iter().any(|e| {
                e.issue_number == Some(issue_num) && e.state == "merging"
            })
        }
    }

    /// `coord merge --order <assignment_id>` sourced from the Pipeline panel's
    /// selected issue (the "Start (automated) > Merge" action, #684).
    ///
    /// Unlike `dispatch_merge_queue_merge` (which fires from the MergeQueue
    /// panel and acts on the *currently selected queue entry*), this function
    /// resolves the completed work assignment from the Pipeline selection and
    /// feeds it to the same `coord merge --order` path.  `coord merge` handles
    /// the idempotent enqueue step server-side before merging.
    fn dispatch_merge_automated_for_selected_pipeline_issue(&mut self) -> bool {
        let Some(aid) = self.selected_completed_work_aid() else {
            self.push_toast(
                "Start merge (automated)",
                "No completed work assignment with a branch — cannot enqueue.",
                ToastSeverity::Warning,
            );
            return false;
        };
        // Guard: refuse if an interactive --merge-of session is already running
        // for this issue — running both would race on the same branch.
        let issue_num = self
            .selected_issue_repo_and_key()
            .map(|(_, k)| k.1)
            .unwrap_or(0);
        if self.has_active_interactive_merge_for_issue(issue_num) {
            self.push_toast(
                "Start merge (automated)",
                "An interactive merge session is already running for this issue — \
                 stop it first to avoid a branch race.",
                ToastSeverity::Warning,
            );
            return false;
        }
        let cmd_strs: Vec<String> = vec![
            "merge".to_string(),
            "--order".to_string(),
            aid.clone(),
        ];
        let cmd_refs: Vec<&str> = cmd_strs.iter().map(|s| s.as_str()).collect();
        use crate::commands::SpawnQueuedOutcome;
        match self.command_runner.spawn_queued(&cmd_refs) {
            SpawnQueuedOutcome::Started => {
                self.push_toast(
                    "Merge queued (automated)",
                    &format!("coord merge --order {}", &aid[..aid.len().min(8)]),
                    ToastSeverity::Info,
                );
                true
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "merge runs after current command",
                    ToastSeverity::Info,
                );
                true
            }
            SpawnQueuedOutcome::Deduped => false,
        }
    }

    /// Launch `coord assign --interactive --merge-of <assignment_id>` in the
    /// standalone Terminal pane (SidebarView::Terminal), reusing the same PTY
    /// infrastructure as Chat/Troubleshoot modes.
    fn launch_merge_queue_interactive(&mut self) {
        // Prefer merge_plan (v0.4.53+ daemon); fall back to raw merge_queue.
        let (work_aid, repo_github, issue_num): (String, String, u64) =
            if !self.data.merge_plan.is_empty() {
                match self.selected_merge_plan_entry() {
                    Some(e) => (e.assignment_id.clone(), e.repo_github.clone(), e.issue_number),
                    None => {
                        self.push_toast(
                            "Resolve interactively",
                            "No entry selected.",
                            ToastSeverity::Warning,
                        );
                        return;
                    }
                }
            } else {
                match self.selected_merge_queue_entry() {
                    Some(e) => (
                        e.assignment_id.clone(),
                        e.repo_github.clone(),
                        e.issue_number.unwrap_or(0),
                    ),
                    None => {
                        self.push_toast(
                            "Resolve interactively",
                            "No entry selected.",
                            ToastSeverity::Warning,
                        );
                        return;
                    }
                }
            };

        // Reverse-lookup: repo_github → coord local repo name.
        let repo: String = self
            .data
            .pipeline_repos
            .iter()
            .find(|(_, gh)| **gh == repo_github)
            .map(|(name, _)| name.clone())
            .unwrap_or_default();
        let machine = self.data.local_machine.clone();

        // Resolve the local checkout path for the terminal's CWD.
        let cwd: std::path::PathBuf = if repo.is_empty() {
            std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("/"))
        } else if let Some(path_str) = self.data.pipeline_repo_paths.get(&repo) {
            std::path::PathBuf::from(path_str)
        } else {
            std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("/"))
        };

        let cfg_path = self
            .command_runner
            .config_path
            .as_ref()
            .map(|p| p.to_string_lossy().into_owned());

        // Build the launch command.
        let launch_line = build_interactive_launch_cmd(
            cfg_path.as_deref(),
            &machine,
            if repo.is_empty() { "unknown-repo" } else { &repo },
            issue_num,
            InteractiveLaunchMode::Merge,
            Some(&work_aid),
        );

        let (cols, rows) = self
            .terminal_pending_dims
            .get()
            .unwrap_or((80, 24));
        let shell = quadraui::terminal_engine::default_shell();

        match quadraui::terminal_engine::TerminalSession::spawn(
            cols.max(20),
            rows.max(5),
            &shell,
            &cwd,
            10_000,
        ) {
            Ok(mut sess) => {
                sess.send_str(&launch_line);
                self.terminal_session = Some(sess);
                self.terminal_spawn_error = None;
                self.terminal_focused = true;
                self.active_view = SidebarView::Terminal;
                self.push_toast(
                    "Resolve interactively",
                    &format!("Launched merge agent for #{}", issue_num),
                    ToastSeverity::Info,
                );
            }
            Err(e) => {
                self.terminal_spawn_error = Some(e.to_string());
                self.push_toast(
                    "Terminal error",
                    &format!("Failed to spawn terminal: {}", e),
                    ToastSeverity::Error,
                );
            }
        }
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
    fn rebuild_pipeline_sidebar(&mut self, prev_sel_override: Option<(String, u64)>) {
        // Preserve selection across rebuilds by (repo_slug, issue#).  Use
        // the caller-provided value when available (it captured before any
        // pipeline_issues swap) and only fall back to the internal capture
        // when the caller didn't touch the list.
        let prev_sel = prev_sel_override.or_else(|| self.capture_pipeline_selection_id());
        // Preserve panel scroll across rebuilds — without this, every 15 s
        // refresh resets the sidebar's scroll to 0 and yanks the visible
        // area back to the top, even when the selection itself is restored
        // correctly.
        let prev_panel_scroll = self.pipeline_sidebar.panel_scroll();
        // Section 0 is always the FILTER form; state sections start at
        // `search_offset`.
        let search_offset = 1usize;
        // Preserve per-section collapse state by state key (section indices
        // may shift if sections appear/disappear between rebuilds, so we key
        // by the state identifier rather than the section index).
        let prev_state_collapsed: std::collections::HashMap<&'static str, bool> = self
            .pipeline_state_section_names
            .iter()
            .enumerate()
            .map(|(i, &name)| (name, self.pipeline_sidebar.is_collapsed(i + search_offset)))
            .collect();

        // Collect unique repo keys in stable order.
        let mut repos: Vec<String> = Vec::new();
        for issue in &self.pipeline_issues {
            let key = Self::pipeline_repo_key(issue).to_string();
            if !repos.contains(&key) {
                repos.push(key);
            }
        }

        // ── Pre-compute pending merge-queue depth per repo slug ────────────
        // Owned map (no lifetime tie to self) so the rest of the function can
        // still call &self and &mut self methods freely.
        let pending_depth_by_slug = self.pending_merge_queue_depth_by_slug();

        // ── Compute the five state buckets ──────────────────────────────
        //
        // #194: lifecycle-driven board — display order matches the issue
        // spec: New → Refining → Pending → In-progress → Done.
        // Active issues stay in a flat list (one node per work assignment).
        // The other four states are grouped by repo with expandable sub-
        // headers, since they routinely accumulate multiple items per repo.
        // Each bucket is computed once here and reused for rows + restore.
        let active_flat: Vec<usize> = self.pipeline_active_issues();
        // main: Live/Idle grouping for the in-progress section (#473/Live-Idle).
        let active_by_liveness: Vec<(String, Vec<usize>)> = self.pipeline_active_by_liveness();
        // #194: the non-Active states split into New / Refining / Pending,
        // each repo-grouped (Pending = the old single "New"/status:ready bucket).
        let new_by_repo: Vec<(String, Vec<usize>)> = self.pipeline_repos_for_state("new");
        let refining_by_repo: Vec<(String, Vec<usize>)> =
            self.pipeline_repos_for_state("refining");
        let pending_by_repo: Vec<(String, Vec<usize>)> = self.pipeline_repos_for_state("pending");
        // #728: Done is now a flat, time-windowed, newest-first list rather
        // than the old repo-grouped archive.
        let done_windowed: Vec<usize> = self.pipeline_done_windowed();
        // Label includes the active window ("Done · last 2h", etc.)
        let done_section_label: String =
            format!("Done · {}", self.done_window.label());

        // Build the list of non-empty state sections in display order.
        // #815: In-progress on top — active work is the most relevant item
        // to see immediately; the pre-dispatch lifecycle states follow in
        // order (New → Refining → Pending); Done stays last.
        let mut state_sections: Vec<(&'static str, &'static str)> = Vec::new();
        if !active_flat.is_empty() {
            state_sections.push(("in-progress", "In-progress"));
        }
        if !new_by_repo.is_empty() {
            state_sections.push(("new", "New"));
        }
        if !refining_by_repo.is_empty() {
            state_sections.push(("refining", "Refining"));
        }
        if !pending_by_repo.is_empty() {
            state_sections.push(("pending", "Pending"));
        }
        if !done_windowed.is_empty() {
            state_sections.push(("done", "Done"));
        }

        // ── Build sidebar section definitions ────────────────────────────
        let mut defs: Vec<SidebarSectionDef> = Vec::new();
        defs.push(SidebarSectionDef::form("pipeline-search", "FILTER"));
        for &(lc_key, _lc_label) in &state_sections {
            // #728: the Done section gets a window-aware label; all others
            // keep their static labels.
            let label = if lc_key == "done" {
                done_section_label.clone()
            } else {
                match lc_key {
                    "new" => "New".to_string(),
                    "refining" => "Refining".to_string(),
                    "pending" => "Pending".to_string(),
                    "in-progress" => "In-progress".to_string(),
                    other => other.to_string(),
                }
            };
            let mut def =
                SidebarSectionDef::new(format!("section:state:{}", lc_key), label);
            def.show_chevron = true;
            def.size = SectionSize::Content;
            defs.push(def);
        }

        let mut sidebar = SidebarSystem::new(defs);
        sidebar.set_navigation_mode(NavigationMode::Selection);
        sidebar.set_allow_collapse(true);
        sidebar.set_scroll_mode(ScrollMode::WholePanel);

        // Populate search form (section 0).
        sidebar.set_form(
            0,
            self.pipeline_search
                .form("pipeline-search", "Filter issues…"),
        );

        // Colour palette per lifecycle state — mirrors the Board's
        // Backlog / Refining / Refined / In-flight / Completed palette
        // for visual consistency between the Board and Pipeline panels.
        let state_color = |lc: &str| match lc {
            "in-progress" => Color::rgb(80, 220, 80),
            "done" => Color::rgb(120, 180, 120),
            "pending" => Color::rgb(140, 180, 240),
            "refining" => Color::rgb(200, 170, 90), // amber — refinement in flight
            "new" => Color::rgb(160, 160, 200),     // muted — pre-pipeline / no label
            _ => Color::rgb(140, 140, 160),
        };

        // ── Populate rows for each state section ─────────────────────────
        for (state_idx, &(lc_key, _lc_label)) in state_sections.iter().enumerate() {
            let section_idx = state_idx + search_offset;
            let mut rows: Vec<TreeRow> = Vec::new();

            match lc_key {
                "in-progress" => {
                    // Active: two collapsible groups — **Live** (a claude
                    // session is running in tmux, local or remote) and **Idle**
                    // (in-progress, no session — waiting on you).  Mirrors the
                    // repo-grouped tree the New/Done sections use; the group key
                    // is the liveness bucket instead of a repo.  Each issue row
                    // keeps the single-letter repo tag so the repo is still
                    // legible when repos are mixed within a group.
                    sidebar.set_section_badge(
                        section_idx,
                        Some(StyledText::plain(format!("({})", active_flat.len()))),
                    );
                    for (gi, (group_key, issue_idxs)) in active_by_liveness.iter().enumerate() {
                        let is_expanded = self
                            .pipeline_lifecycle_expanded
                            .get(&("in-progress".to_string(), group_key.clone()))
                            .copied()
                            .unwrap_or(true);
                        let header_color = if group_key == "live" {
                            Color::rgb(80, 160, 240) // blue = active session (matches accent_bg)
                        } else {
                            Color::rgb(150, 150, 160) // dim = idle
                        };
                        rows.push(TreeRow {
                            path: vec![gi as u16],
                            indent: 1,
                            icon: None,
                            text: StyledText {
                                spans: vec![StyledSpan::with_fg(
                                    format!(
                                        "{} ({})",
                                        Self::liveness_group_label(group_key),
                                        issue_idxs.len()
                                    ),
                                    header_color,
                                )],
                            },
                            badge: None,
                            is_expanded: Some(is_expanded),
                            decoration: Decoration::Header,
                            edit: None,
                        });
                        if !is_expanded {
                            continue;
                        }
                        for (ii, &issue_idx) in issue_idxs.iter().enumerate() {
                            let issue = &self.pipeline_issues[issue_idx];
                            let tag = Self::repo_tag(Self::pipeline_repo_key(issue), &repos);
                            let tag_color = Color::rgb(180, 140, 240);
                            let title_color = if issue.coord_repo.is_some() {
                                Color::rgb(210, 210, 210)
                            } else {
                                Color::rgb(140, 140, 140)
                            };
                            rows.push(TreeRow {
                                path: vec![gi as u16, ii as u16],
                                indent: 2,
                                icon: None,
                                text: StyledText {
                                    spans: vec![
                                        StyledSpan::with_fg(
                                            format!("#{:<5}", issue.number),
                                            Color::rgb(150, 150, 240),
                                        ),
                                        StyledSpan::with_fg(
                                            trunc(&issue.title, 20),
                                            title_color,
                                        ),
                                    ],
                                },
                                badge: Some(Badge::colored(tag, tag_color)),
                                is_expanded: None,
                                decoration: Decoration::Normal,
                                edit: None,
                            });
                        }
                    }
                }
                "done" => {
                    // #728: Done is now a flat, time-windowed, newest-first list.
                    // No repo sub-headers; issue rows directly at [0, ii].
                    // Each row shows: #N  title  status  age  [● live]
                    sidebar.set_section_badge(
                        section_idx,
                        Some(StyledText::plain(format!("({})", done_windowed.len()))),
                    );
                    let now_secs = std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_secs_f64();
                    for (ii, &issue_idx) in done_windowed.iter().enumerate() {
                        let issue = &self.pipeline_issues[issue_idx];
                        let title_color = if issue.coord_repo.is_some() {
                            Color::rgb(160, 160, 160)
                        } else {
                            Color::rgb(110, 110, 110)
                        };
                        // Terse status: ✓ merged / ✓ closed
                        let status_str = if self.merge_stage_status_for(issue) == StageStatus::Done {
                            "✓ merged"
                        } else {
                            "✓ closed"
                        };
                        // Relative age.
                        let age_str = match self.issue_done_at(issue) {
                            Some(t) => {
                                let secs = (now_secs - t).max(0.0) as u64;
                                if secs < 3600 {
                                    format!("  {}m ago", secs / 60)
                                } else if secs < 86_400 {
                                    format!("  {}h ago", secs / 3600)
                                } else {
                                    format!("  {}d ago", secs / 86_400)
                                }
                            }
                            None => String::new(),
                        };
                        let mut spans = vec![
                            StyledSpan::with_fg(
                                format!("#{:<5}", issue.number),
                                Color::rgb(150, 150, 240),
                            ),
                            StyledSpan::with_fg(trunc(&issue.title, 18), title_color),
                            StyledSpan::with_fg(
                                format!("  {}", status_str),
                                Color::rgb(100, 180, 100),
                            ),
                            StyledSpan::with_fg(age_str, Color::rgb(120, 120, 140)),
                        ];
                        // ● session live badge (#728).
                        if self.issue_session_is_live(issue) {
                            spans.push(StyledSpan::with_fg(
                                "  ● live".to_string(),
                                Color::rgb(80, 160, 240),
                            ));
                        }
                        // Repo tag badge (same as in-progress, for orientation).
                        let tag = Self::repo_tag(Self::pipeline_repo_key(issue), &repos);
                        rows.push(TreeRow {
                            path: vec![0u16, ii as u16],
                            indent: 2,
                            icon: None,
                            text: StyledText { spans },
                            badge: Some(Badge::colored(tag, Color::rgb(180, 140, 240))),
                            is_expanded: None,
                            decoration: Decoration::Normal,
                            edit: None,
                        });
                    }
                }
                _ => {
                    // New / Refining / Pending: grouped by repo with
                    // expandable repo sub-headers.  #194 expanded this from
                    // the original three sections to all four non-Active
                    // lifecycle states.
                    //
                    // #668: New additionally groups by milestone beneath
                    // the repo level (repo → milestone → issue, 3-level path
                    // [ri, mi, ii]).  Refining and Pending remain 2-level.
                    let repo_groups: &Vec<(String, Vec<usize>)> = match lc_key {
                        "new" => &new_by_repo,
                        "refining" => &refining_by_repo,
                        "pending" => &pending_by_repo,
                        // `state_sections` is built in this function and only
                        // ever contains the five known keys above — this arm
                        // is unreachable.
                        _ => unreachable!("invalid lifecycle key in state_sections"),
                    };
                    let total: usize = repo_groups.iter().map(|(_, v)| v.len()).sum();
                    sidebar.set_section_badge(
                        section_idx,
                        Some(StyledText::plain(format!("({})", total))),
                    );
                    let milestone_grouped = lc_key == "new";
                    for (ri, (repo_key, issue_idxs)) in repo_groups.iter().enumerate() {
                        // Repo sub-header: expand/collapse keyed by
                        // (lifecycle_key, repo_key) — lifecycle first, repo
                        // second, matching the new field comment semantics.
                        let is_expanded = self
                            .pipeline_lifecycle_expanded
                            .get(&(lc_key.to_string(), repo_key.clone()))
                            .copied()
                            .unwrap_or(true);
                        // ── #526: pending merge-queue depth badge ─────────
                        // Resolve coord-local repo key → GitHub slug, then
                        // look up the pre-computed depth for this repo.
                        let repo_slug = self
                            .data
                            .pipeline_repos
                            .iter()
                            .find(|(coord, _)| coord == repo_key)
                            .map(|(_, slug)| slug.as_str())
                            .unwrap_or(repo_key.as_str());
                        let queue_depth = pending_depth_by_slug
                            .get(repo_slug)
                            .copied()
                            .unwrap_or(0);
                        // Show a badge when there are pending queue entries:
                        //   > 5 → amber warning
                        //   1–5 → muted indicator
                        //   0   → no badge
                        let queue_badge = if queue_depth > 5 {
                            Some(Badge::colored(
                                format!("Q:{}", queue_depth),
                                Color::rgb(220, 160, 40), // amber
                            ))
                        } else if queue_depth > 0 {
                            Some(Badge::colored(
                                format!("Q:{}", queue_depth),
                                Color::rgb(160, 140, 100), // muted
                            ))
                        } else {
                            None
                        };
                        rows.push(TreeRow {
                            path: vec![ri as u16],
                            indent: 1,
                            icon: None,
                            text: StyledText {
                                spans: vec![StyledSpan::with_fg(
                                    format!("{} ({})", repo_key, issue_idxs.len()),
                                    state_color(lc_key),
                                )],
                            },
                            badge: queue_badge,
                            is_expanded: Some(is_expanded),
                            decoration: Decoration::Header,
                            edit: None,
                        });
                        if !is_expanded {
                            continue;
                        }

                        if milestone_grouped {
                            // #668: New — 3-level tree: repo → milestone → issue.
                            // Compute milestones for this repo's issue list.
                            let milestones =
                                self.pipeline_milestones_for_issues(issue_idxs);
                            for (mi, (mil_key, mil_display, mil_issue_idxs)) in
                                milestones.iter().enumerate()
                            {
                                let is_mil_expanded = self
                                    .pipeline_milestone_expanded
                                    .get(&(
                                        lc_key.to_string(),
                                        repo_key.clone(),
                                        mil_key.clone(),
                                    ))
                                    .copied()
                                    .unwrap_or(true);
                                let mil_color = if mil_key == "no-milestone" {
                                    Color::rgb(100, 100, 120) // dim for unassigned
                                } else {
                                    Color::rgb(160, 160, 200) // muted purple for named
                                };
                                rows.push(TreeRow {
                                    path: vec![ri as u16, mi as u16],
                                    indent: 2,
                                    icon: None,
                                    text: StyledText {
                                        spans: vec![StyledSpan::with_fg(
                                            format!(
                                                "{} ({})",
                                                mil_display,
                                                mil_issue_idxs.len()
                                            ),
                                            mil_color,
                                        )],
                                    },
                                    badge: None,
                                    is_expanded: Some(is_mil_expanded),
                                    decoration: Decoration::Header,
                                    edit: None,
                                });
                                if !is_mil_expanded {
                                    continue;
                                }
                                for (ii, &issue_idx) in mil_issue_idxs.iter().enumerate() {
                                    let issue = &self.pipeline_issues[issue_idx];
                                    let stage_name = self.derive_current_stage(issue);
                                    let (badge_text, badge_color) = stage_badge(&stage_name, &self.active_theme);
                                    let title_color = if issue.coord_repo.is_some() {
                                        Color::rgb(210, 210, 210)
                                    } else {
                                        Color::rgb(140, 140, 140)
                                    };
                                    let has_live_stream = self.watch_pool.values().any(|ctx| {
                                        ctx.state.issue_number == issue.number && !ctx.sse.done
                                    });
                                    let mut spans = vec![
                                        StyledSpan::with_fg(
                                            format!("#{:<5}", issue.number),
                                            Color::rgb(150, 150, 240),
                                        ),
                                        StyledSpan::with_fg(
                                            trunc(&issue.title, 20),
                                            title_color,
                                        ),
                                    ];
                                    if has_live_stream {
                                        spans.push(StyledSpan::with_fg(
                                            " ▶".to_string(),
                                            Color::rgb(60, 200, 80),
                                        ));
                                    }
                                    rows.push(TreeRow {
                                        path: vec![ri as u16, mi as u16, ii as u16],
                                        indent: 3,
                                        icon: None,
                                        text: StyledText { spans },
                                        badge: Some(Badge::colored(&badge_text, badge_color)),
                                        is_expanded: None,
                                        decoration: Decoration::Normal,
                                        edit: None,
                                    });
                                }
                            }
                        } else {
                            // Refining / Pending — 2-level tree: repo → issue.
                            for (ii, &issue_idx) in issue_idxs.iter().enumerate() {
                                let issue = &self.pipeline_issues[issue_idx];
                                let stage_name = self.derive_current_stage(issue);
                                let (badge_text, badge_color) = stage_badge(&stage_name, &self.active_theme);
                                let title_color = if issue.coord_repo.is_some() {
                                    Color::rgb(210, 210, 210)
                                } else {
                                    Color::rgb(140, 140, 140)
                                };
                                let has_live_stream = self
                                    .watch_pool
                                    .values()
                                    .any(|ctx| ctx.state.issue_number == issue.number && !ctx.sse.done);
                                let mut spans = vec![
                                    StyledSpan::with_fg(
                                        format!("#{:<5}", issue.number),
                                        Color::rgb(150, 150, 240),
                                    ),
                                    StyledSpan::with_fg(trunc(&issue.title, 20), title_color),
                                ];
                                if has_live_stream {
                                    spans.push(StyledSpan::with_fg(
                                        " ▶".to_string(),
                                        Color::rgb(60, 200, 80),
                                    ));
                                }
                                rows.push(TreeRow {
                                    path: vec![ri as u16, ii as u16],
                                    indent: 2,
                                    icon: None,
                                    text: StyledText { spans },
                                    badge: Some(Badge::colored(&badge_text, badge_color)),
                                    is_expanded: None,
                                    decoration: Decoration::Normal,
                                    edit: None,
                                });
                            }
                        }
                    }
                }
            }
            sidebar.set_rows(section_idx, rows);
        }

        // Default-select the first issue in the first non-empty state section.
        if sidebar.active_section().is_none() && !state_sections.is_empty() {
            let section_idx = search_offset; // first state section
            sidebar.set_active_section(Some(section_idx));
            // Active / Done / Refining / Pending use path [group_idx, issue_idx] (2-level).
            // New uses path [repo_idx, milestone_idx, issue_idx] (3-level, #668).
            // #728: Done is now flat [0, issue_idx] (2-level), not 3-level.
            let first_state_key = state_sections.get(0).map(|&(k, _)| k).unwrap_or("");
            let default_path = if first_state_key == "new" {
                vec![0u16, 0u16, 0u16]
            } else {
                vec![0u16, 0u16]
            };
            sidebar.set_selected_path(section_idx, Some(default_path));
        }

        self.pipeline_repo_names = repos;
        let new_state_section_names: Vec<&'static str> =
            state_sections.iter().map(|&(k, _)| k).collect();
        self.pipeline_state_section_names = new_state_section_names;
        self.pipeline_sidebar = sidebar;

        // Capture the previous issue number.  Used to decide whether the
        // focused-stage index needs to be recomputed (we only reset on issue
        // change, not on every refresh).
        let prev_issue_num: Option<u64> = prev_sel.as_ref().map(|(_, num)| *num);

        // Restore previous selection if the issue still exists in the new
        // layout.  Search both the flat Active list and the repo-grouped
        // New/Done lists using the pre-computed buckets.
        if let Some((repo, num)) = prev_sel {
            'outer: for (state_idx, &state_key) in
                self.pipeline_state_section_names.iter().enumerate()
            {
                let section_idx = state_idx + search_offset;
                if state_key == "in-progress" {
                    // Active: liveness-grouped — path = [group_idx, issue_idx].
                    for (gi, (_gk, issue_idxs)) in active_by_liveness.iter().enumerate() {
                        for (ii, &idx) in issue_idxs.iter().enumerate() {
                            let issue = &self.pipeline_issues[idx];
                            if issue.repo_slug == repo && issue.number == num {
                                self.pipeline_sel = Some(idx);
                                self.pipeline_sidebar.set_active_section(Some(section_idx));
                                self.pipeline_sidebar.set_selected_path(
                                    section_idx,
                                    Some(vec![gi as u16, ii as u16]),
                                );
                                break 'outer;
                            }
                        }
                    }
                } else if state_key == "done" {
                    // #728: Done is now flat [0, issue_idx] — search
                    // pipeline_done_windowed() directly (no repo/milestone levels).
                    for (ii, &idx) in done_windowed.iter().enumerate() {
                        let issue = &self.pipeline_issues[idx];
                        if issue.repo_slug == repo && issue.number == num {
                            self.pipeline_sel = Some(idx);
                            self.pipeline_sidebar.set_active_section(Some(section_idx));
                            self.pipeline_sidebar.set_selected_path(
                                section_idx,
                                Some(vec![0u16, ii as u16]),
                            );
                            break 'outer;
                        }
                    }
                } else {
                    let repo_groups: &Vec<(String, Vec<usize>)> = match state_key {
                        "new" => &new_by_repo,
                        "refining" => &refining_by_repo,
                        "pending" => &pending_by_repo,
                        // Unknown state key — skip selection restore.
                        _ => continue,
                    };
                    // #668: New uses 3-level paths [ri, mi, ii];
                    // Refining and Pending remain 2-level [ri, ii].
                    if state_key == "new" {
                        for (ri, (_, issue_idxs)) in repo_groups.iter().enumerate() {
                            // Compute milestones; owned Vec, so no self borrow persists.
                            let milestones = self.pipeline_milestones_for_issues(issue_idxs);
                            for (mi, (_, _, mil_issue_idxs)) in milestones.iter().enumerate() {
                                for (ii, &idx) in mil_issue_idxs.iter().enumerate() {
                                    let issue = &self.pipeline_issues[idx];
                                    if issue.repo_slug == repo && issue.number == num {
                                        self.pipeline_sel = Some(idx);
                                        self.pipeline_sidebar
                                            .set_active_section(Some(section_idx));
                                        self.pipeline_sidebar.set_selected_path(
                                            section_idx,
                                            Some(vec![ri as u16, mi as u16, ii as u16]),
                                        );
                                        break 'outer;
                                    }
                                }
                            }
                        }
                    } else {
                        for (ri, (_, issue_idxs)) in repo_groups.iter().enumerate() {
                            for (ii, &idx) in issue_idxs.iter().enumerate() {
                                let issue = &self.pipeline_issues[idx];
                                if issue.repo_slug == repo && issue.number == num {
                                    self.pipeline_sel = Some(idx);
                                    self.pipeline_sidebar
                                        .set_active_section(Some(section_idx));
                                    self.pipeline_sidebar.set_selected_path(
                                        section_idx,
                                        Some(vec![ri as u16, ii as u16]),
                                    );
                                    break 'outer;
                                }
                            }
                        }
                    }
                }
            }
        }
        // Sync `pipeline_sel` to the sidebar's actual selection.
        self.pipeline_sel = self.selected_pipeline_index();
        // Restore per-section collapse state by state key.  New sections
        // that weren't present before default to expanded, EXCEPT Done which
        // defaults to collapsed (#815) — it's an archive, not active work.
        for (i, &state_key) in self.pipeline_state_section_names.iter().enumerate() {
            if let Some(&was_collapsed) = prev_state_collapsed.get(state_key) {
                self.pipeline_sidebar
                    .set_collapsed(i + search_offset, was_collapsed);
            } else if state_key == "done" {
                // #815: Done section starts collapsed by default so the active
                // sections are immediately visible without scrolling past history.
                self.pipeline_sidebar.set_collapsed(i + search_offset, true);
            }
        }
        // Restore panel scroll so the visible area doesn't jump back to the
        // top on every refresh.  Done after selection restore so the active-
        // section's keyboard nav state is set up correctly before scroll is
        // applied.
        self.pipeline_sidebar.set_panel_scroll(prev_panel_scroll);
        // Auto-focus: when the selected issue changes (or on first render
        // with no previous selection), default to the smart stage rather
        // than leaving `pipeline_focused_stage = None`.  When the same
        // issue is re-selected after a data refresh, keep the focus stable
        // so a 15 s refresh doesn't clobber the user's explicit choice.
        let new_issue_num = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))
            .map(|iss| iss.number);
        if new_issue_num != prev_issue_num {
            self.pipeline_focused_stage = self.default_focused_stage_for_selected_issue();
            self.pipeline_stage_content_scroll = 0;
        }
        // Re-apply filter focus after rebuild so the cursor survives auto-refresh.
        if self.pipeline_search.focused {
            self.pipeline_sidebar.focus_form(0, true);
        }
    }

    /// Resolve the SidebarSystem's current selection to a `pipeline_issues`
    /// index.
    ///
    /// Path depth varies by state:
    /// - `in-progress`: `[group_idx, issue_idx]` (2-level)
    /// - `new`: `[repo_idx, milestone_idx, issue_idx]` (3-level, #668)
    /// - `done`: `[0, issue_idx]` (2-level flat, #728 — no repo/milestone groups)
    /// - `refining` / `pending`: `[repo_idx, issue_idx]` (2-level)
    ///
    /// A path shorter than the minimum for the state (header row selected)
    /// returns `None`.
    fn selected_pipeline_index(&self) -> Option<usize> {
        // Section 0 is the FILTER form; state sections start at search_offset.
        let search_offset = 1usize;
        let section = self.pipeline_sidebar.active_section()?;
        if section < search_offset {
            return None; // search section selected, not a state section
        }
        let state_idx = section - search_offset;
        let &state_key = self.pipeline_state_section_names.get(state_idx)?;
        let path = self.pipeline_sidebar.selected_path(section)?;

        if state_key == "new" {
            // #668: 3-level path [repo_idx, milestone_idx, issue_idx].
            if path.len() < 3 {
                return None; // repo or milestone header selected
            }
            let ri = path[0] as usize;
            let mi = path[1] as usize;
            let ii = path[2] as usize;
            let repo_groups = self.pipeline_repos_for_state(state_key);
            let (_, repo_issue_idxs) = repo_groups.get(ri)?;
            let milestones = self.pipeline_milestones_for_issues(repo_issue_idxs);
            let (_, _, mil_issue_idxs) = milestones.get(mi)?;
            mil_issue_idxs.get(ii).copied()
        } else if state_key == "done" {
            // #728: flat 2-level path [0, issue_idx] — path[0] is the
            // synthetic group (always 0); path[1] is the windowed list index.
            if path.len() < 2 {
                return None;
            }
            let ii = path[1] as usize;
            let done_windowed = self.pipeline_done_windowed();
            done_windowed.get(ii).copied()
        } else if state_key == "in-progress" {
            if path.len() < 2 {
                return None; // liveness group header selected
            }
            let gi = path[0] as usize;
            let ii = path[1] as usize;
            let groups = self.pipeline_active_by_liveness();
            let (_, issue_idxs) = groups.get(gi)?;
            issue_idxs.get(ii).copied()
        } else {
            // refining / pending — 2-level [repo_idx, issue_idx].
            if path.len() < 2 {
                return None; // repo sub-header selected
            }
            let gi = path[0] as usize;
            let ii = path[1] as usize;
            let groups = self.pipeline_repos_for_state(state_key);
            let (_, issue_idxs) = groups.get(gi)?;
            issue_idxs.get(ii).copied()
        }
    }

    /// Resolve the per-stage status of an issue from existing assignments.
    ///
    /// "work" is the first stage and matches assignments with
    /// `assignment_type` `None` or `"work"`.  Other stage names match
    /// assignments by exact `assignment_type`.  The "merge" stage is
    /// special-cased to read from the `merge_queue` table instead, since
    /// merges are not modelled as assignments.
    fn stage_status_for(&self, issue: &PipelineIssue, stage: &str) -> StageStatus {
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
            let mapped = match latest.status.as_str() {
                // #473: the Review stage colour must reflect the VERDICT, not
                // merely that the review process ran (a review always ends
                // status="done").  Keying on status alone painted a
                // `request-changes` verdict GREEN — reading as "approved" and
                // inviting a premature merge.  Consult `review_verdict`:
                //   approve         → Done   (green ✓)
                //   request-changes → Failed (red ✗ — changes requested)
                //   None/unknown    → Failed (red ✗ — #812: a terminal done
                //                     row with no verdict is a dead end, not
                //                     in-progress.  Renders as recoverable
                //                     Failed rather than permanent blue Active)
                // Note: in-flight reviews (status="running") are handled by
                // the `any(status=="running")` guard at the top of this
                // function and never reach this match arm.
                "done" if stage == "review" => match latest.review_verdict.as_deref() {
                    Some("approve") => Some(StageStatus::Done),
                    Some("request-changes") | Some("fail") => Some(StageStatus::Failed),
                    _ => Some(StageStatus::Failed),
                },
                "done" => Some(StageStatus::Done),
                "failed" => Some(StageStatus::Failed),
                _ => None,
            };
            if let Some(v) = mapped {
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
    fn upstream_max_dispatched_at(&self, issue: &PipelineIssue, stage: &str) -> Option<f64> {
        let names = self.pipeline_stage_names_for_issue(issue);
        let idx = names.iter().position(|s| s == stage)?;
        if idx == 0 {
            return None;
        }
        names[..idx]
            .iter()
            .flat_map(|s| self.assignments_for_stage(issue, s))
            .filter_map(|a| a.dispatched_at)
            .fold(None, |acc, t| Some(acc.map_or(t, |x: f64| x.max(t))))
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
        // #290: if a merge was just dispatched from the Go button, optimistically
        // return Active immediately so the Merge box turns blue and the Go button
        // disappears — without waiting for the next DB refresh to land the real
        // merge_queue entry.  The flag is cleared in apply_pending_data once the
        // entry exists in the DB, at which point the real state takes over.
        if self
            .pipeline_inflight_merges
            .contains(&(issue.repo_slug.clone(), issue.number))
        {
            let has_real_entry = self
                .data
                .merge_queue
                .iter()
                .any(|m| m.issue_number == Some(issue.number) && m.repo_github == issue.repo_slug);
            if !has_real_entry {
                return StageStatus::Active;
            }
        }
        let entry = self
            .data
            .merge_queue
            .iter()
            .find(|m| m.issue_number == Some(issue.number) && m.repo_github == issue.repo_slug);
        match entry.map(|e| e.state.as_str()) {
            Some("merged") => StageStatus::Done,
            Some("open") | Some("queued") => StageStatus::Active,
            // #241: HUMAN_REQUIRED (failed conflict-fix) — the merge needs a
            // human, so Failed.  `failed` (legacy / direct) is also Failed.
            Some("failed") | Some("human_required") => StageStatus::Failed,
            _ => {
                // A pending (not yet merged/active) entry whose PR has failing
                // CI checks goes Failed so the Merge box is red at a glance —
                // the user shouldn't have to open the detail row or wait for a
                // GitHub email to learn a check broke.
                if entry.is_some_and(|e| self.ci_failed_for_entry(e)) {
                    StageStatus::Failed
                } else if self.data.assignments.iter().any(|a| {
                    // #775: the daemon's merge-reconcile tick prunes the queue
                    // row after flipping the work assignment to status='merged'.
                    // Once the row is gone the match above falls here, so we
                    // also check the assignment itself — a merged work assignment
                    // is sufficient evidence that the Merge stage is Done, even
                    // with no surviving queue entry.
                    a.issue_number == issue.number
                        && issue
                            .coord_repo
                            .as_deref()
                            .map(|r| r == a.repo)
                            .unwrap_or(true)
                        && a.assignment_type.as_deref() == Some("work")
                        && a.status == "merged"
                }) {
                    StageStatus::Done
                } else if issue.is_closed {
                    StageStatus::Skipped
                } else {
                    StageStatus::Pending
                }
            }
        }
    }

    /// True when *entry*'s PR has a fetched CI summary with failing checks.
    /// Looks up `pipeline_ci_checks` by `(repo_github, pr_number)`; returns
    /// false when the entry has no PR yet or no summary has been fetched.
    fn ci_failed_for_entry(&self, entry: &MergeQueueEntry) -> bool {
        let Some(pr) = entry.pr_number else {
            return false;
        };
        self.pipeline_ci_checks
            .get(&(entry.repo_github.clone(), pr))
            .is_some_and(|s| s.has_failures())
    }

    /// #241: is there a conflict-fix worker currently in flight for *issue*?
    fn has_active_conflict_fix(&self, issue: &PipelineIssue) -> bool {
        self.data.assignments.iter().any(|a| {
            a.issue_number == issue.number
                && a.assignment_type.as_deref() == Some("conflict-fix")
                && (a.status == "running" || a.status == "pending")
        })
    }

    /// #585: is there a manual/interactive smoke (or test-chat) session in
    /// flight for *issue*?  Mirrors [`has_active_conflict_fix`] for the Merge
    /// box: while the operator is re-verifying, the Test box should read blue
    /// (Active) — even over a prior automated `passed` verdict — so the green
    /// box doesn't imply the verdict is already in when it isn't yet.  The
    /// interactive testing agent (`--smoke-of`) is `type="smoke"`; the
    /// conversational gate is `type="test-chat"`.
    fn has_active_smoke_session(&self, issue: &PipelineIssue) -> bool {
        self.data.assignments.iter().any(|a| {
            a.issue_number == issue.number
                && issue.coord_repo.as_deref().map_or(true, |r| a.repo == r)
                && matches!(
                    a.assignment_type.as_deref(),
                    Some("smoke") | Some("test-chat")
                )
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
            return if issue.is_closed {
                StageStatus::Skipped
            } else {
                StageStatus::Pending
            };
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
        // #585: a manual/interactive smoke session in flight keeps the Test box
        // blue (Active) — even over a prior `passed` verdict — so the operator
        // sees the gate is being re-evaluated, not already decided.  Resolves to
        // green/red once the session ends and its verdict is the latest.
        if self.has_active_smoke_session(issue) {
            return StageStatus::Active;
        }
        // #310: a review bounce creates a new fix-work assignment that carries
        // an *empty* test_state. The test genuinely passed on the earlier work,
        // so don't revert Test to Pending (which would strand the Merge "Go"
        // button via prior_all_done). Resolve the verdict from the most recent
        // work assignment that actually carries one. A later re-test that
        // failed will itself carry test_state="failed" and win as the most
        // recent verdict; an in-flight re-test is handled above.
        let verdict = work
            .iter()
            .filter(|a| {
                a.test_state
                    .as_deref()
                    .map(|s| !s.is_empty())
                    .unwrap_or(false)
            })
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .and_then(|a| a.test_state.as_deref());
        match verdict {
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
        if issue.is_closed {
            StageStatus::Skipped
        } else {
            StageStatus::Pending
        }
    }

    /// Returns the *display* current stage for the sidebar badge — the
    /// first non-Done/non-Skipped stage, or "done" once every meaningful
    /// stage is Done or Skipped.
    ///
    /// Skipped stages (closed-issue stages that never ran through coord) are
    /// treated the same as Done for badge purposes: they don't represent a
    /// meaningful "current" action and should not halt the badge at "work".
    ///
    /// The lifecycle section is used as the source of truth for the "done"
    /// state: if `pipeline_lifecycle_section` returns `"done"`, the badge
    /// always reads "done" regardless of individual stage statuses.  This
    /// prevents two categories of inconsistency:
    ///
    /// 1. **Open-but-merged issues** (`merge_stage_status_for == Done`, GitHub
    ///    issue still open): `stage_status_for` returns `Pending` (not
    ///    `Skipped`) for stages with no assignment because `is_closed` is
    ///    `false`.  Without the short-circuit, `derive_current_stage` would
    ///    halt at "review" or "work" and the sidebar badge would read "review"
    ///    or "work" for an issue that the coordinator has already merged.
    ///
    /// 2. **Stale downstream stages on closed issues**: if a work assignment
    ///    was re-dispatched after a review completed, the review stage becomes
    ///    `Stale` (not `Done` or `Skipped`), which would again halt the badge
    ///    at "review" even though the issue is closed.
    fn derive_current_stage(&self, issue: &PipelineIssue) -> String {
        // Lifecycle section is the authoritative summary.  If the issue is
        // already in "done", the badge must read "done" — never a bare stage
        // name that would be visually indistinguishable from a lifecycle label.
        if self.pipeline_lifecycle_section(issue) == "done" {
            return "done".to_string();
        }
        let stages = self.pipeline_stage_names();
        for s in &stages {
            let st = self.stage_status_for(issue, s);
            if st != StageStatus::Done && st != StageStatus::Skipped {
                return s.clone();
            }
        }
        "done".to_string()
    }

    /// Compute the default focused-stage index for the currently-selected
    /// pipeline issue.  Called whenever the selection changes so the
    /// Pipeline tab always opens on the most relevant content without
    /// requiring the user to click or press `[`/`]` first.
    ///
    /// - **In-progress** issues → first stage that is not Done or Skipped
    ///   (the "current" stage the user is waiting on).
    /// - **Done** issues (all stages Done/Skipped) → last stage index
    ///   (typically merge), so the user immediately sees the completion detail.
    /// - Returns `None` when no issue is selected or the issue has no stages.
    fn default_focused_stage_for_selected_issue(&self) -> Option<usize> {
        let idx = self.pipeline_sel?;
        let issue = self.pipeline_issues.get(idx)?;
        let stages = self.pipeline_stage_names_for_issue(issue);
        if stages.is_empty() {
            return None;
        }
        // First stage that is not yet finished — that's the "current" stage.
        for (i, name) in stages.iter().enumerate() {
            let status = self.stage_status_for(issue, name);
            if status != StageStatus::Done && status != StageStatus::Skipped {
                return Some(i);
            }
        }
        // All stages settled → point at the last one so the user sees the
        // final outcome (merge details, review verdict, etc.) immediately.
        Some(stages.len() - 1)
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
        let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i)) else {
            return;
        };
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
        let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i)) else {
            return;
        };
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

    /// #264: True when the currently-open `inject_chat` overlay is bound to
    /// a `type="refinement"` assignment for a specific issue (rather than a
    /// board-level or worker-guidance session).  Refinement chats render
    /// inline in the Pipeline Refinement tab so tab-switching keeps working.
    ///
    /// #316: Board-level chats (`issue_number == 0`) are excluded here and
    /// handled separately by `chat_is_board_chat()`.
    fn chat_is_refinement(&self) -> bool {
        if self.inject_chat.is_none() {
            return false;
        }
        self.focused_watch_state()
            .map(|w| w.assignment_type == "refinement" && w.issue_number > 0)
            .unwrap_or(false)
    }

    /// #316: True when the currently-open `inject_chat` overlay is a
    /// board-level chat (`issue_number == 0`).  Board chats render inline in
    /// `BoardDetailTab::Chat` rather than a Pipeline Refinement tab.  Covers
    /// both `type="new-issue-chat"` and board `type="refinement"` sessions.
    fn chat_is_board_chat(&self) -> bool {
        if self.inject_chat.is_none() {
            return false;
        }
        self.focused_watch_state()
            .map(|w| w.issue_number == 0)
            .unwrap_or(false)
    }

    /// #264: True when any `type="refinement"` assignment is currently
    /// running on the selected pipeline issue.  Drives the Refinement
    /// tab's accent dot so the tab is discoverable without forcing focus.
    fn has_active_refinement_for_selected_issue(&self) -> bool {
        let Some(sel) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(sel) else {
            return false;
        };
        self.data.assignments.iter().any(|a| {
            a.issue_number == issue.number
                && a.assignment_type.as_deref() == Some("refinement")
                && a.status == "running"
        })
    }

    /// #264: Placeholder rendered in the Refinement tab when no chat is
    /// bound to the current issue.  Keeps the tab a useful surface even
    /// when empty — tells the user how to start one.
    fn refinement_tab_placeholder_list(&self) -> ListView {
        let items = vec![
            kv_item("", "", None),
            kv_item(
                "",
                "  No refinement chat is open for this issue.",
                Some(Color::rgb(180, 180, 200)),
            ),
            kv_item("", "", None),
            kv_item(
                "",
                "  To start one, right-click the issue on the Board panel and pick",
                Some(Color::rgb(140, 140, 160)),
            ),
            kv_item(
                "",
                "  \"Refine with chat\".  A claude -p worker will be seeded with the",
                Some(Color::rgb(140, 140, 160)),
            ),
            kv_item(
                "",
                "  issue + CLAUDE.md + repo file tree; this tab will switch to the",
                Some(Color::rgb(140, 140, 160)),
            ),
            kv_item(
                "",
                "  chat UI as soon as the worker is ready.",
                Some(Color::rgb(140, 140, 160)),
            ),
        ];
        ListView {
            id: WidgetId::new("refinement-placeholder"),
            title: None,
            items,
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: false,
            bordered: true,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    /// #303: The single button rendered in the pipeline button bar above the
    /// stage row.  Returns `(label, stage_index)` for the stage that owns the
    /// action (today: at most one `[Go]` or `[Retry]` per pipeline), or
    /// `None` when no stage is dispatchable.
    ///
    /// Uses the same widget builder as `dispatch_pipeline_active_go` so the
    /// bar and the Enter keybind dispatch the exact same stage.
    fn pipeline_action_button(&self) -> Option<(String, usize)> {
        let view = self.build_pipeline_widget()?;
        view.stages
            .iter()
            .enumerate()
            .find_map(|(i, s)| s.action.as_ref().map(|label| (label.clone(), i)))
    }

    /// Build the [`Toolbar`] for the pipeline action bar (the `[ Go ⏎ ]` /
    /// `[ Retry ⏎ ]` strip above the stage boxes) when any stage is
    /// dispatchable.  Returns `None` when no stage is actionable.
    ///
    /// Extracted so the render path and the mouse-move hover-update path
    /// share one definition and can't drift apart.
    fn pipeline_action_bar_toolbar(&self) -> Option<Toolbar> {
        let (label, _stage_idx) = self.pipeline_action_button()?;
        Some(Toolbar {
            focused_index: None,
            id: WidgetId::new("pipeline-action-bar"),
            buttons: vec![ToolbarButton::Action {
                id: WidgetId::new("pipeline-action:dispatch"),
                label: label.clone(),
                icon: None,
                key_hint: Some("⏎".to_string()),
                enabled: true,
                is_active: false,
                tooltip: format!(
                    "Dispatch {} for the active stage (Enter)",
                    label.to_lowercase()
                ),
            }],
            bg: None,
        })
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
                    if let Some(work_id) = self
                        .assignments_for_stage(issue, "work")
                        .iter()
                        .max_by(|a, b| {
                            a.dispatched_at
                                .partial_cmp(&b.dispatched_at)
                                .unwrap_or(std::cmp::Ordering::Equal)
                        })
                        .map(|a| a.id.clone())
                    {
                        if self.test_build_in_flight(&work_id) {
                            label = "Building".to_string();
                        }
                    }
                }
                // Show turn count + elapsed on Active stage box.
                // Prefer live SSE turn count when watching, fall back to local log.
                // Elapsed is computed from dispatched_at to now (wall-clock).
                if status == StageStatus::Active {
                    let running = self
                        .assignments_for_stage(issue, name)
                        .into_iter()
                        .find(|a| a.status == "running");
                    if let Some(a) = running {
                        // Use cached SSE turn count from any pool entry for
                        // this assignment (not just the focused stream), so
                        // the badge updates even for background watches.
                        let turns = if let Some(ctx) = self.watch_pool.get(&a.id) {
                            if ctx.sse.current_turn > 0 {
                                ctx.sse.current_turn
                            } else {
                                self.turn_count_from_log(&a.id)
                            }
                        } else {
                            self.turn_count_from_log(&a.id)
                        };
                        if turns > 0 {
                            label = format!("{} T{}", label, turns);
                        }
                        // Elapsed since dispatch — second line in the box.
                        if let Some(dispatched_f) = a.dispatched_at {
                            let now_secs = std::time::SystemTime::now()
                                .duration_since(std::time::UNIX_EPOCH)
                                .map(|d| d.as_secs_f64())
                                .unwrap_or(dispatched_f);
                            let elapsed = (now_secs - dispatched_f).max(0.0) as u64;
                            label = format!("{}\n{}", label, fmt_elapsed_mmss(elapsed));
                        }
                    }
                }
                // Skipped counts as "settled" for prior_all_done: a closed-issue
                // stage that never ran is logically done.
                let prior_all_done = statuses[..i]
                    .iter()
                    .all(|s| *s == StageStatus::Done || *s == StageStatus::Skipped);
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
        let focused_stage = self.pipeline_focused_stage.filter(|&i| i < stages.len());
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
            .filter(|m| !self.paused_machines.contains(&m.name))
            .min_by_key(|m| m.active_count)
    }

    /// #200: Find the latest Work assignment id for the currently-selected
    /// pipeline issue. Returns None if no issue is selected or no work assignment
    /// exists yet.
    fn pipeline_selected_work_id(&self) -> Option<String> {
        let issue = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))?;
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
                        work_id.chars().take(8).collect::<String>(),
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
        let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i)) else {
            return false;
        };
        let stages = self.pipeline_stage_names();
        if !stages.iter().any(|s| s == "test") {
            return false;
        }
        self.test_stage_status_for(issue) == StageStatus::Pending
            && self.stage_status_for_internal_work(issue) == StageStatus::Done
    }

    /// #349: True when the Pipeline view is active, an issue is selected, and
    /// the currently focused stage is "test".  Used to gate the 1–9 step-run
    /// keybindings so they don't fire on unrelated digit presses elsewhere.
    fn is_test_stage_focused(&self) -> bool {
        let Some(sel_idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(sel_idx) else {
            return false;
        };
        let stage_names = self.pipeline_stage_names_for_issue(issue);
        self.pipeline_focused_stage
            .and_then(|fi| stage_names.get(fi).map(|n| n.as_str() == "test"))
            .unwrap_or(false)
    }

    /// #349: Return the original step index of the first `kind: "pull"` step
    /// in the test plan for the currently-selected pipeline issue.  Returns
    /// `None` when no issue is selected, no plan is loaded, or the plan has
    /// no pull step.  Used by the `[a]` keybind handler to route pull steps
    /// through `run_test_plan_step` rather than the artifact-badge path.
    fn test_plan_pull_step_idx(&self) -> Option<usize> {
        let sel_idx = self.pipeline_sel?;
        let issue = self.pipeline_issues.get(sel_idx)?;
        let local_repo = issue.coord_repo.as_deref();
        let work = self
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
            })?;
        work.test_plan
            .as_ref()?
            .iter()
            .position(|s| s.kind == "pull")
    }

    /// #349: Return the original step index of the `key_num`-th non-pull step
    /// (1-indexed) in the test plan for the currently-selected pipeline issue.
    ///
    /// Pull steps are assigned the `[a]` keybind and excluded from the
    /// 1–9 numbering.  Pressing `3` maps to the 3rd step whose kind is NOT
    /// `"pull"`, regardless of how many pull steps precede it.
    ///
    /// Returns `None` when no plan is loaded or `key_num` exceeds the count
    /// of non-pull steps.
    fn test_plan_runnable_step_idx(&self, key_num: usize) -> Option<usize> {
        let sel_idx = self.pipeline_sel?;
        let issue = self.pipeline_issues.get(sel_idx)?;
        let local_repo = issue.coord_repo.as_deref();
        let work = self
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
            })?;
        let mut count = 0usize;
        for (i, step) in work.test_plan.as_ref()?.iter().enumerate() {
            if step.kind != "pull" {
                count += 1;
                if count == key_num {
                    return Some(i);
                }
            }
        }
        None
    }

    // ── #336 artifact helpers ────────────────────────────────────────────────

    /// #336: Resolve the artifact fetch target for the currently-selected
    /// pipeline issue.  Returns `(agent_host, repo, sanitized_branch, work_id)`
    /// when a work assignment with a known branch + machine is found, or `None`.
    fn artifact_fetch_target(&self) -> Option<(String, String, String, String)> {
        let issue = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))?;
        let work = self.assignments_for_stage(issue, "work");
        let latest = work.iter().max_by(|a, b| {
            a.dispatched_at
                .partial_cmp(&b.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        })?;
        let branch = latest.branch.as_ref()?;
        let sanitized = sanitize_branch(branch);
        let host = self
            .data
            .machines
            .iter()
            .find(|m| m.name == latest.machine)
            .map(|m| m.host.clone())?;
        if host.is_empty() {
            return None;
        }
        Some((host, latest.repo.clone(), sanitized, latest.id.clone()))
    }

    /// #336: True when the selected pipeline issue has a non-empty artifact
    /// manifest in the 30-second TTL cache — i.e. the artifact badge is
    /// currently rendered and the `a` keybind should be live.
    fn artifact_badge_visible(&self) -> bool {
        let issue = match self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i)) {
            Some(i) => i,
            None => return false,
        };
        let work = self.assignments_for_stage(issue, "work");
        let latest = match work.iter().max_by(|a, b| {
            a.dispatched_at
                .partial_cmp(&b.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        }) {
            Some(a) => a,
            None => return false,
        };
        let branch = match &latest.branch {
            Some(b) => b,
            None => return false,
        };
        let key = (latest.repo.clone(), sanitize_branch(branch));
        self.artifact_cache
            .get(&key)
            .and_then(|e| e.manifest.as_ref())
            .map(|m| !m.files.is_empty())
            .unwrap_or(false)
    }

    /// #532: Decide what pressing `a` on the selected pipeline row should do
    /// for the artifact flow.  Read-only — does not spawn commands or mutate
    /// state.  Production calls this from the `Key::Char('a')` arm and
    /// dispatches the returned action; tests call it directly to verify the
    /// routing matches each of the three cases (cached, badge, absence).
    ///
    /// Returns `None` when there's no work assignment in scope, no badge,
    /// no cached pull, and no recorded absence reason — i.e. the keybind
    /// would be a no-op.
    fn compute_a_key_artifact_action(&self) -> Option<AKeyArtifactAction> {
        let (_, repo, sanitized, work_id) = self.artifact_fetch_target()?;
        // 1. Existing pull result → re-open the dialog (highest priority so
        //    `a` doesn't kick off a redundant re-pull on a known result).
        if let Some(pull) = self.last_artifact_pulls.get(&work_id).cloned() {
            let dlg = if pull.exit_code == 0 {
                ArtifactPullDialog {
                    path: Some(pull.message.clone()),
                    body: format!("Saved to:\n{}", pull.message),
                }
            } else {
                ArtifactPullDialog {
                    path: None,
                    body: format!(
                        "Pull failed (exit {}):\n{}",
                        pull.exit_code, pull.message
                    ),
                }
            };
            return Some(AKeyArtifactAction::ReopenDialog(dlg));
        }
        // 2. Badge visible (manifest non-empty) → start a pull.
        if self.artifact_badge_visible() {
            return Some(AKeyArtifactAction::StartPull {
                work_id,
                repo,
                sanitized,
            });
        }
        // 3. No badge but a known absence reason → explain it.  An empty
        //    stash is the common, confusing case (#563/#569): distinguish a
        //    non-building CLI change (nothing to pull, ever) from a build that
        //    should have happened but didn't.
        let produces_artifact = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))
            .map(|iss| issue_produces_build_artifact(&repo, &iss.title))
            .unwrap_or(true);
        let cache_key = (repo, sanitized.clone());
        let body = self
            .artifact_cache
            .get(&cache_key)
            .and_then(|e| e.absence_reason.as_ref())
            .map(|absence| match absence {
                ArtifactAbsence::NotStashed | ArtifactAbsence::ManifestEmpty => {
                    artifact_absence_body(produces_artifact, &sanitized)
                }
                ArtifactAbsence::AgentUnreachable(e) => {
                    let msg: String = e.chars().take(200).collect();
                    format!("Agent unreachable:\n{}", msg)
                }
            })?;
        Some(AKeyArtifactAction::ShowAbsence(ArtifactPullDialog {
            path: None,
            body,
        }))
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
        let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i)) else {
            return false;
        };
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
        let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i)) else {
            return false;
        };
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
            let _ = tx.send(TestBuildOutcome {
                exit_code,
                first_error,
            });
        });

        self.push_toast(
            "Phase 1 build started",
            &format!("#{} on {} — fetching and building…", issue_number, branch),
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
                Ok(TestBuildOutcome {
                    exit_code: code,
                    first_error,
                }) => {
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
                            job.issue_number,
                            job.log_path.display()
                        ),
                        ToastSeverity::Error,
                    );
                }
            }
        }
        true
    }

    // ── #349: Test-plan lifecycle ─────────────────────────────────────────────

    /// #349: Spawn `coord test-plan [--refresh] <work_id>` via CommandRunner
    /// when the test stage is focused and no plan is cached (or it is stale).
    ///
    /// Called from `run_periodic_work` each tick.  Idempotent: will not spawn
    /// a second time while the first is still in-flight (CommandRunner is
    /// single-slot and the work_id will be in `test_plan_pending`).
    fn maybe_spawn_test_plan(&mut self) {
        // Only act when the Pipeline view is active and a test stage is focused.
        if self.active_view != SidebarView::Pipeline {
            return;
        }
        let Some(sel_idx) = self.pipeline_sel else {
            return;
        };
        let Some(issue) = self.pipeline_issues.get(sel_idx).cloned() else {
            return;
        };

        // Determine which stage is focused and whether it's the "test" stage.
        let stage_names = self.pipeline_stage_names_for_issue(&issue);
        let focused_is_test = self
            .pipeline_focused_stage
            .and_then(|fi| stage_names.get(fi).map(|n| n.as_str() == "test"))
            .unwrap_or(false);
        if !focused_is_test {
            return;
        }

        // Find the latest work assignment for this issue.
        let local_repo = issue.coord_repo.as_deref();
        let work = self
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
            .cloned();
        let Some(work) = work else {
            return;
        };
        let work_id = work.id.clone();

        // ── Staleness check ────────────────────────────────────────────────
        // Run once per work_id (tracked by test_plan_staleness_checked_for).
        if self.test_plan_staleness_checked_for.as_deref() != Some(&work_id) {
            self.test_plan_staleness_checked_for = Some(work_id.clone());
            // Only check staleness when a plan AND a branch_head exist.
            if let (Some(branch), Some(cached_head)) = (&work.branch, &work.test_plan_branch_head) {
                if let Some(local_path) = issue
                    .coord_repo
                    .as_ref()
                    .and_then(|r| self.data.pipeline_repo_paths.get(r.as_str()))
                {
                    let repo_dir = std::path::Path::new(local_path.as_str());
                    if let Some(live_head) = read_git_branch_head(repo_dir, branch) {
                        if &live_head != cached_head {
                            // Branch advanced — refresh the plan.
                            self.test_plan_pending.insert(work_id.clone());
                            self.command_runner
                                .spawn_queued(&["test-plan", &work_id, "--refresh"]);
                            return;
                        }
                    }
                }
            }
        }

        // ── Spawn when plan is missing ─────────────────────────────────────
        if work.test_plan.is_none() && !self.test_plan_pending.contains(&work_id) {
            self.test_plan_pending.insert(work_id.clone());
            self.command_runner.spawn_queued(&["test-plan", &work_id]);
        }
    }

    /// #349: Run the test-plan step at *step_idx* for the selected pipeline
    /// issue's latest work assignment.  Spawns a background thread that
    /// executes the step's shell command and sends the exit code back via an
    /// mpsc channel, which is polled by `poll_test_step_jobs`.
    ///
    /// No-ops when:
    /// - No work assignment or test plan is available.
    /// - `step_idx` is out of range for the plan.
    /// - A job for `(work_id, step_idx)` is already in flight.
    fn run_test_plan_step(&mut self, step_idx: usize) {
        let Some(sel_idx) = self.pipeline_sel else {
            return;
        };
        let Some(issue) = self.pipeline_issues.get(sel_idx).cloned() else {
            return;
        };
        let local_repo = issue.coord_repo.as_deref();
        let work = self
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
            .cloned();
        let Some(work) = work else {
            return;
        };
        let Some(ref steps) = work.test_plan else {
            return;
        };
        let Some(step) = steps.get(step_idx) else {
            return;
        };
        let work_id = work.id.clone();
        let key = (work_id.clone(), step_idx);

        // Don't re-run an already in-flight step.
        if self.test_step_jobs.contains_key(&key) {
            return;
        }

        // "verify" steps have no command — just mark checked (exit 0).
        if step.kind == "verify" {
            self.test_step_results.insert(key, 0);
            return;
        }

        let Some(ref cmd_str) = step.cmd else {
            // No command to run — mark as checked.
            self.test_step_results.insert(key, 0);
            return;
        };

        let cmd_owned = cmd_str.clone();
        let (tx, rx) = std::sync::mpsc::channel::<(i32, String)>();
        std::thread::spawn(move || {
            use std::process::{Command, Stdio};
            // Capture both stdout and stderr so the output can be displayed
            // inline in the test stage panel.  Bounded to 64 KiB combined to
            // prevent unbounded memory growth from verbose commands.
            const MAX_OUTPUT: usize = 64 * 1024;
            let result = Command::new("sh")
                .arg("-c")
                .arg(&cmd_owned)
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .output();
            let (exit_code, output_str) = match result {
                Ok(out) => {
                    let code = out.status.code().unwrap_or(-1);
                    let mut combined = String::new();
                    // Stdout first, then stderr, with a separator when both are non-empty.
                    let stdout = String::from_utf8_lossy(&out.stdout);
                    let stderr = String::from_utf8_lossy(&out.stderr);
                    if !stdout.is_empty() {
                        combined.push_str(&stdout);
                    }
                    if !stderr.is_empty() {
                        if !combined.is_empty() {
                            combined.push_str("\n── stderr ──\n");
                        }
                        combined.push_str(&stderr);
                    }
                    // Truncate to MAX_OUTPUT bytes (safe: truncate at char boundary).
                    if combined.len() > MAX_OUTPUT {
                        let truncated: String = combined
                            .chars()
                            .take(
                                combined
                                    .char_indices()
                                    .take_while(|(b, _)| *b < MAX_OUTPUT)
                                    .count(),
                            )
                            .collect();
                        combined = truncated;
                        combined.push_str("\n… (output truncated)");
                    }
                    (code, combined)
                }
                Err(e) => (-1, format!("failed to spawn: {e}")),
            };
            let _ = tx.send((exit_code, output_str));
        });

        self.test_step_jobs.insert(
            key,
            TestStepJob {
                work_id,
                step_idx,
                rx,
            },
        );
        self.push_toast(
            &format!("Step {}", step_idx + 1),
            &format!("Running: {}", cmd_str.chars().take(80).collect::<String>()),
            ToastSeverity::Info,
        );
    }

    /// #349: Drain completed test-plan step jobs.  Returns `true` when at
    /// least one job finished (the panel needs to be redrawn).
    fn poll_test_step_jobs(&mut self) -> bool {
        if self.test_step_jobs.is_empty() {
            return false;
        }
        use std::sync::mpsc::TryRecvError;
        let mut done: Vec<(String, usize, i32, String)> = Vec::new();
        for (key, job) in self.test_step_jobs.iter() {
            match job.rx.try_recv() {
                Ok((exit_code, output)) => done.push((key.0.clone(), key.1, exit_code, output)),
                Err(TryRecvError::Disconnected) => {
                    done.push((key.0.clone(), key.1, -1, String::new()))
                }
                Err(TryRecvError::Empty) => {}
            }
        }
        if done.is_empty() {
            return false;
        }
        for (work_id, step_idx, exit_code, output) in done {
            self.test_step_jobs.remove(&(work_id.clone(), step_idx));
            self.test_step_results
                .insert((work_id.clone(), step_idx), exit_code);
            if !output.is_empty() {
                self.test_step_output.insert((work_id, step_idx), output);
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
        let Some(sel) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(sel).cloned() else {
            return false;
        };
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
                format!("no failed {} assignment found for #{}", stage, issue.number),
                Instant::now(),
            ));
            return false;
        };
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&["retry", &id]);
        match outcome {
            SpawnQueuedOutcome::Started => {
                self.pipeline_status = Some((
                    format!("retry dispatched for {} #{}", stage, issue.number),
                    Instant::now(),
                ));
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "retry runs after current command",
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Deduped => {}
        }
        matches!(
            outcome,
            SpawnQueuedOutcome::Started | SpawnQueuedOutcome::Queued
        )
    }

    /// Dispatch the Work stage.
    ///
    /// If a completed Plan assignment exists for this issue, runs
    /// `coord approve-plan <plan_id>` — which uses the plan output as the
    /// briefing for the new work assignment.  Otherwise falls back to a
    /// fresh `coord assign <machine> <repo> <issue>`.
    ///
    /// #685: always arms the test-mode choice dialog first; the dialog's
    /// confirm path calls `dispatch_pipeline_work_with_mode` to do the
    /// actual dispatch after `coord set-test-mode` runs.
    fn dispatch_pipeline_work(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
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

        // For the approve-plan path we still need a machine to be reachable.
        let machine_name = if self.find_done_plan_assignment_id(&issue, &coord_repo).is_none() {
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
            Some(machine.name.clone())
        } else {
            None // approve-plan resolves its own machine
        };

        // Inject session-level model override when the user configured one for
        // this machine in Settings → Dispatch → Per-Machine Model Overrides.
        let model_str = machine_name.as_ref().and_then(|mn| {
            self.settings
                .machine_model
                .get(mn)
                .map(|p| p.as_str().to_string())
        });

        // Read the current test-mode label (if any) to pre-select the default.
        let current_mode = issue
            .all_labels
            .iter()
            .find(|l| l.starts_with("test-mode:"))
            .map(|l| l.trim_start_matches("test-mode:").to_string());

        // #685: arm the test-mode choice dialog.  The dialog's confirm path
        // will call `dispatch_pipeline_work_with_mode` with the chosen mode.
        self.pending_test_mode_choice = Some(PendingTestModeChoice {
            coord_repo,
            issue_num: issue.number,
            action: TestModeChoiceAction::DispatchWork,
            current_mode,
            machine_name,
            model_override: model_str,
        });
        true
    }

    /// #685: called by the test-mode dialog confirm path.  Dispatches the
    /// headless Work assignment after `coord set-test-mode` has run.
    fn dispatch_pipeline_work_with_mode(
        &mut self,
        coord_repo: &str,
        issue_num: u64,
        machine_name: Option<&str>,
        model_override: Option<&str>,
    ) -> bool {
        use crate::commands::SpawnQueuedOutcome;

        // Resolve the issue from pipeline_issues to check for a done plan.
        let issue_clone = self
            .pipeline_issues
            .iter()
            .find(|i| i.number == issue_num && i.coord_repo.as_deref() == Some(coord_repo))
            .cloned();

        // If a done plan exists for this issue, approve it.
        if let Some(ref issue) = issue_clone {
            if let Some(plan_id) = self.find_done_plan_assignment_id(issue, coord_repo) {
                let outcome = self.command_runner.spawn_queued(&["approve-plan", &plan_id]);
                match outcome {
                    SpawnQueuedOutcome::Started => {
                        self.pipeline_status = Some((
                            format!(
                                "approving plan {} → dispatching work for #{}",
                                &plan_id[..plan_id.len().min(8)],
                                issue_num
                            ),
                            Instant::now(),
                        ));
                    }
                    SpawnQueuedOutcome::Queued => {
                        self.push_toast(
                            "⏳ Queued",
                            "approve-plan runs after current command",
                            ToastSeverity::Info,
                        );
                    }
                    SpawnQueuedOutcome::Deduped => {}
                }
                return matches!(
                    outcome,
                    SpawnQueuedOutcome::Started | SpawnQueuedOutcome::Queued
                );
            }
        }

        let Some(mn) = machine_name else {
            self.pipeline_status = Some((
                format!("no machine for {} (issue #{})", coord_repo, issue_num),
                Instant::now(),
            ));
            return false;
        };

        let issue_str = issue_num.to_string();
        let mut cmd: Vec<String> =
            vec!["assign".into(), mn.to_string(), coord_repo.to_string(), issue_str];
        if let Some(m) = model_override {
            cmd.push("--model".into());
            cmd.push(m.to_string());
        }
        let cmd_refs: Vec<&str> = cmd.iter().map(|s| s.as_str()).collect();
        let outcome = self.command_runner.spawn_queued(&cmd_refs);
        match outcome {
            SpawnQueuedOutcome::Started => {
                self.pipeline_status = Some((
                    format!("dispatched #{} → {}", issue_num, mn),
                    Instant::now(),
                ));
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "assign runs after current command",
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Deduped => {}
        }
        matches!(
            outcome,
            SpawnQueuedOutcome::Started | SpawnQueuedOutcome::Queued
        )
    }

    /// Dispatch the Plan stage: `coord assign --plan-only <machine> <repo> <issue>`.
    fn dispatch_pipeline_plan(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
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
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&cmd_refs);
        match outcome {
            SpawnQueuedOutcome::Started => {
                self.pipeline_status = Some((
                    format!("plan dispatched for #{} → {}", issue.number, machine_name),
                    Instant::now(),
                ));
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "plan assign runs after current command",
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Deduped => {}
        }
        matches!(
            outcome,
            SpawnQueuedOutcome::Started | SpawnQueuedOutcome::Queued
        )
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
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&["notify"]);
        match outcome {
            SpawnQueuedOutcome::Started => {
                self.pipeline_status = Some((
                    format!("notify dispatched for #{}", issue.number),
                    Instant::now(),
                ));
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "notify runs after current command",
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Deduped => {}
        }
        matches!(
            outcome,
            SpawnQueuedOutcome::Started | SpawnQueuedOutcome::Queued
        )
    }

    /// #486: refresh the Pipeline's data source.
    ///
    /// The Pipeline now reads from the local DB cache (`data.open_issues`,
    /// rebuilt into `pipeline_issues` in `apply_pending_data`), the SAME source
    /// the Board uses.  So "refresh" means: kick a background `coord sync` (gh
    /// `issue list` — core API, 5000/hr, consistent; throttled) to update the
    /// cache, then reload `data` from the DB so the rebuilt pipeline reflects
    /// it.  No live `gh search` (the old 30/min, eventually-consistent, several-
    /// seconds path) — the pipeline now renders instantly from already-loaded
    /// data and never silently goes stale on a Search-API rate-limit.
    fn maybe_kick_pipeline_loader(&mut self) {
        self.kick_issue_sync();
        self.refresh();
    }

    /// Kick off background `gh pr checks` polls for any PR in the merge
    /// queue without a fresh CI summary on hand.  No-op outside the Pipeline
    /// view.
    ///
    /// Three guards prevent the thundering-herd that fired when ~45 PRs all
    /// went stale at the same 30-second mark:
    ///
    /// 1. **Concurrency cap** — at most `CI_MAX_IN_FLIGHT` loaders run at
    ///    once; excess targets wait for a free slot in a later tick.
    /// 2. **Per-tick stagger** — at most `CI_MAX_NEW_PER_TICK` new loaders
    ///    are started per call, spreading bursts across many ticks.
    /// 3. **TTL tiering** — cache TTL is based on observed CI state (see
    ///    [`ci_stale_secs`]): running CI → 30s, terminal (pass/fail) → 600s,
    ///    no cache + eligible → fetch immediately, no cache + ineligible →
    ///    skip entirely (CI result irrelevant until review clears).
    fn maybe_kick_ci_check_loaders(&mut self) {
        if self.active_view != SidebarView::Pipeline {
            return;
        }

        /// Maximum concurrent `gh pr checks` subprocesses.
        const CI_MAX_IN_FLIGHT: usize = 4;
        /// Maximum new loaders started in a single tick (stagger guard).
        const CI_MAX_NEW_PER_TICK: usize = 2;

        // Global cap: don't wake any new threads when already at the limit.
        if self.pipeline_ci_loader.len() >= CI_MAX_IN_FLIGHT {
            return;
        }

        // Whether this project has a review gate (if not, every PR is eligible).
        let has_review_stage = self.pipeline_stage_names().iter().any(|s| s == "review");

        // Snapshot the queue — collect repo/PR pairs plus merge-eligibility so
        // we don't hold an immutable borrow while mutating pipeline_ci_loader.
        let targets: Vec<(String, i64, bool)> = self
            .data
            .merge_queue
            .iter()
            .filter_map(|m| {
                let pr = m.pr_number?;
                if m.repo_github.is_empty() {
                    return None;
                }
                // A PR is merge-eligible when it has cleared the review gate
                // (approved verdict on any assignment for the same issue).
                let merge_eligible = if !has_review_stage {
                    true
                } else if let Some(issue_num) = m.issue_number {
                    self.data.assignments.iter().any(|a| {
                        a.issue_number == issue_num
                            && a.review_verdict.as_deref() == Some("approve")
                    })
                } else {
                    false
                };
                Some((m.repo_github.clone(), pr, merge_eligible))
            })
            .collect();

        let mut kicked = 0;
        for (repo, pr, merge_eligible) in targets {
            // Per-tick stagger: never start more than CI_MAX_NEW_PER_TICK
            // loaders in one call, regardless of how many are due.
            if kicked >= CI_MAX_NEW_PER_TICK {
                break;
            }
            // Global cap re-check inside the loop (some slots may have been
            // filled by earlier iterations of this same tick).
            if self.pipeline_ci_loader.len() >= CI_MAX_IN_FLIGHT {
                break;
            }
            let key = (repo.clone(), pr);
            if self.pipeline_ci_loader.contains_key(&key) {
                continue;
            }
            // CI-state-based TTL tiering (see `ci_stale_secs`):
            // - running CI      → 30s  (needs timely updates)
            // - terminal CI     → 600s (won't change; check rarely)
            // - no cache + eligible  → fetch now
            // - no cache + ineligible → skip entirely (blocked on review)
            let needs_refresh =
                match ci_stale_secs(self.pipeline_ci_checks.get(&key), merge_eligible) {
                    None => false,
                    Some(threshold_secs) => match self.pipeline_ci_checks.get(&key) {
                        Some(cached) => {
                            cached.fetched_at.elapsed() >= Duration::from_secs(threshold_secs)
                        }
                        None => true, // threshold_secs == 0 for eligible+no-cache → fetch now
                    },
                };
            if !needs_refresh {
                continue;
            }
            let (tx, rx) = std::sync::mpsc::channel();
            std::thread::spawn(move || {
                let _ = tx.send(fetch_ci_check_summary(&repo, pr));
            });
            self.pipeline_ci_loader.insert(key, rx);
            kicked += 1;
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
                // Toast only on the *transition* into failure: the new summary
                // has failures and either there was no prior summary or the
                // prior summary was clean.  Without the transition guard we'd
                // re-toast every 30s poll while CI stays red.
                let prev_failed = self
                    .pipeline_ci_checks
                    .get(&key)
                    .is_some_and(|s| s.has_failures());
                if summary.has_failures() && !prev_failed {
                    let (repo_github, pr) = &key;
                    let names = summary.failed_names.join(", ");
                    let body = if names.is_empty() {
                        format!("{repo_github} #{pr}")
                    } else {
                        format!("{repo_github} #{pr} — {names}")
                    };
                    self.push_toast("⚠ CI failed", &body, ToastSeverity::Warning);
                }
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
        let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i)) else {
            return false;
        };
        // Need a queued merge entry that is not yet merged.
        let entry = self
            .data
            .merge_queue
            .iter()
            .find(|m| m.issue_number == Some(issue.number) && m.repo_github == issue.repo_slug);
        let Some(entry) = entry else {
            return false;
        };
        if entry.state == "merged" {
            return false;
        }
        // Only consider issues whose pipeline includes a "review" stage —
        // a project without review gates shouldn't see this hint.
        let stages = self.pipeline_stage_names();
        if !stages.iter().any(|s| s == "review") {
            return false;
        }
        // #292 (Defect 1/2): mirror coord/merge_queue.py::has_approved_review.
        // After a review bounce the entry may be keyed to the *original* work
        // assignment while the approved re-review is linked to the *fix* work
        // assignment.  Collect ALL work IDs for this issue and accept an
        // approval against any of them.  Seed with entry.assignment_id so
        // reviews are found even when the work row is pruned from the DB.
        let approved = self.issue_has_any_approved_review(issue, Some(&entry.assignment_id));
        !approved
    }

    /// Returns true when any review assignment for *issue* carries
    /// `review_verdict="approve"`.
    ///
    /// #292: used by `merge_blocked_on_review_for_selected_issue` and
    /// `pipeline_merge_state` to accept an approval on a *fix* work
    /// assignment even when the merge entry is still keyed to the original
    /// work assignment.  Mirrors `coord/merge_queue.py::has_approved_review`.
    ///
    /// *seed_work_id*: the queue entry's `assignment_id`, included in the
    /// set of candidate work IDs even when the corresponding assignment row
    /// has been pruned from `data.assignments` (old rows GC'd from the local
    /// DB).  Pass `None` when no queue entry is involved (the no-queue path
    /// in `pipeline_merge_state`).
    fn issue_has_any_approved_review(
        &self,
        issue: &PipelineIssue,
        seed_work_id: Option<&str>,
    ) -> bool {
        // Collect all work assignment IDs for this issue from the local DB.
        let mut work_ids: std::collections::HashSet<&str> = self
            .data
            .assignments
            .iter()
            .filter(|a| {
                a.issue_number == issue.number && a.assignment_type.as_deref() == Some("work")
            })
            .map(|a| a.id.as_str())
            .collect();

        // Also seed with the queue entry's assignment_id so reviews that
        // point to it are found even when the corresponding work assignment
        // row has been pruned from data.assignments.
        if let Some(id) = seed_work_id {
            work_ids.insert(id);
        }

        // #331: self-approval / PR-comment-fallback — review_verdict='approve'
        // may be stamped directly on a work assignment when no separate review
        // worker was dispatched (coordinator manual approval) or when GitHub
        // rejected `gh pr review` as a self-review (findings posted to the
        // issue comment instead, verdict still persisted to the DB).
        //
        // Treat review_verdict='approve' on any work assignment in work_ids as
        // approved — this matches the intent of coord/merge_queue.py's
        // has_approved_review: the DB verdict is the source of truth regardless
        // of whether a formal GitHub PR review exists.
        if self.data.assignments.iter().any(|a| {
            work_ids.contains(a.id.as_str()) && a.review_verdict.as_deref() == Some("approve")
        }) {
            return true;
        }

        if work_ids.is_empty() {
            return false;
        }

        self.data.assignments.iter().any(|a| {
            a.assignment_type.as_deref() == Some("review")
                && a.issue_number == issue.number
                && a.review_of_assignment_id
                    .as_deref()
                    .map(|id| work_ids.contains(id))
                    .unwrap_or(false)
                && a.review_verdict.as_deref() == Some("approve")
        })
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
        let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i)) else {
            return PipelineMergeState::NotApplicable;
        };
        let Some(entry) = self
            .data
            .merge_queue
            .iter()
            .find(|m| m.issue_number == Some(issue.number) && m.repo_github == issue.repo_slug)
        else {
            // #292 (Defect 2): no queue entry yet — this is normal when
            // notify/auto_loop haven't had a chance to create the entry.
            // If work is done and review is approved, let `coord merge`
            // handle the auto-enqueue rather than showing a misleading
            // "no PR queued — work hasn't pushed a branch" toast.
            let stages = self.pipeline_stage_names();
            if stages.iter().any(|s| s == "review")
                && self.issue_has_any_approved_review(issue, None)
            {
                return PipelineMergeState::Ready {
                    issue: issue.number,
                    repo: issue.coord_repo.clone().unwrap_or_default(),
                    repo_slug: issue.repo_slug.clone(),
                };
            }
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
        // #292 (Defect 1/2): use issue_has_any_approved_review so a fix-work
        // approval is found even when the entry is keyed to the original work.
        // Seed with entry.assignment_id so reviews are found even if the work
        // row itself has been pruned from data.assignments.
        let stages = self.pipeline_stage_names();
        if stages.iter().any(|s| s == "review") {
            let approved = self.issue_has_any_approved_review(issue, Some(&entry.assignment_id));
            if !approved {
                // Surface the most informative verdict we can find.
                // Prefer "request-changes" over a bare "pending" so the
                // toast tells the user what happened.
                let verdict = self
                    .data
                    .assignments
                    .iter()
                    .filter(|a| {
                        a.assignment_type.as_deref() == Some("review")
                            && a.issue_number == issue.number
                    })
                    .filter_map(|a| a.review_verdict.as_deref())
                    .find(|&v| v == "request-changes")
                    .unwrap_or("pending");
                return PipelineMergeState::BlockedOnReview {
                    issue: issue.number,
                    verdict: verdict.to_string(),
                };
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
            repo_slug: issue.repo_slug.clone(),
        }
    }

    /// #272-followup: route a Merge action (m keybind OR toolbar:merge
    /// button) through the state classifier so silent no-ops are
    /// replaced with actionable toasts.  Returns `true` when something
    /// happened (dispatched, opened a prompt, or surfaced a toast) so
    /// the caller can request a redraw.
    fn dispatch_pipeline_merge_for_selected_issue(&mut self) -> bool {
        use crate::commands::SpawnQueuedOutcome;
        match self.pipeline_merge_state() {
            PipelineMergeState::NotApplicable => {
                // Outside the Pipeline view, fall back to the unscoped
                // "merge whatever's ready" behaviour (matches the
                // pre-classifier `m` keybind on the Board / Machines
                // views).
                match self.command_runner.spawn_queued(&["merge"]) {
                    SpawnQueuedOutcome::Queued => {
                        self.push_toast(
                            "⏳ Queued",
                            "merge runs after current command",
                            ToastSeverity::Info,
                        );
                    }
                    SpawnQueuedOutcome::Deduped | SpawnQueuedOutcome::Started => {}
                }
                true
            }
            PipelineMergeState::NoQueue { issue } => {
                self.push_toast(
                    "Merge",
                    &format!("#{issue}: no PR queued yet — work hasn't pushed a branch."),
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
            PipelineMergeState::Ready { issue, repo, repo_slug } => {
                if repo.is_empty() {
                    // No coord_repo mapping — fall back to unscoped
                    // merge so power users with cross-repo work can
                    // still drive it from the TUI.
                    match self.command_runner.spawn_queued(&["merge"]) {
                        SpawnQueuedOutcome::Started => {
                            // #347: no coord_repo so we can't key the inflight
                            // set (merge_stage_status_for keys on repo_slug),
                            // but at least show a status-bar toast so there's
                            // some feedback.
                            self.pipeline_status = Some((
                                format!("merge dispatched (#{issue})"),
                                Instant::now(),
                            ));
                        }
                        SpawnQueuedOutcome::Queued => {
                            self.push_toast(
                                "⏳ Queued",
                                "merge runs after current command",
                                ToastSeverity::Info,
                            );
                        }
                        SpawnQueuedOutcome::Deduped => {}
                    }
                } else {
                    match self
                        .command_runner
                        .spawn_queued(&["merge", "--repo", &repo])
                    {
                        SpawnQueuedOutcome::Started => {
                            // #347: mirror the optimistic update that
                            // dispatch_pipeline_merge (per-stage [Go]) does —
                            // set the inflight flag so merge_stage_status_for
                            // returns Active immediately, and surface a toast
                            // via pipeline_status, all within the same frame as
                            // the user action.
                            self.pipeline_inflight_merges
                                .insert((repo_slug, issue));
                            self.pipeline_status = Some((
                                format!("merge dispatched for {} (#{})", repo, issue),
                                Instant::now(),
                            ));
                        }
                        SpawnQueuedOutcome::Queued => {
                            self.push_toast(
                                "⏳ Queued",
                                "merge runs after current command",
                                ToastSeverity::Info,
                            );
                        }
                        SpawnQueuedOutcome::Deduped => {}
                    }
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
        let issue = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))?;
        let entry = self
            .data
            .merge_queue
            .iter()
            .find(|m| m.issue_number == Some(issue.number) && m.repo_github == issue.repo_slug)?;
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
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&["bounce", &review_id]);
        match outcome {
            SpawnQueuedOutcome::Deduped => {
                // Same bounce already running or queued — nothing to add.
                false
            }
            SpawnQueuedOutcome::Started => {
                self.push_toast(
                    "Bounce",
                    &format!(
                        "Dispatching fix worker for review {}\u{2026} Work will go Active in 1-3s once the subprocess completes.",
                        &review_id[..review_id.len().min(8)],
                    ),
                    ToastSeverity::Info,
                );
                true
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "Bounce",
                    &format!(
                        "Bounce queued for review {}\u{2026} Will dispatch once the current command completes.",
                        &review_id[..review_id.len().min(8)],
                    ),
                    ToastSeverity::Info,
                );
                true
            }
        }
    }

    /// Look up the CI summary for the currently-selected pipeline issue's
    /// merge queue entry.  Returns `None` when no PR is queued for the
    /// selected issue, or when no summary has been fetched yet.
    fn ci_summary_for_selected_issue(&self) -> Option<&CiCheckSummary> {
        let issue = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))?;
        let entry = self
            .data
            .merge_queue
            .iter()
            .find(|m| m.issue_number == Some(issue.number) && m.repo_github == issue.repo_slug)?;
        let pr = entry.pr_number?;
        self.pipeline_ci_checks
            .get(&(entry.repo_github.clone(), pr))
    }

    /// Drain the in-flight poll channel without blocking. Returns `true`
    /// when results were received and the issue list/sidebar changed.
    /// #486 Leg 4: drain the background local+remote session fetch.  When it
    /// completes it REPLACES the local-only startup snapshot so the reattach
    /// detection in `launch_interactive_session_for_selected_issue` can target
    /// sessions running on remote fleet machines.  Returns true on update.
    fn poll_remote_sessions(&mut self) -> bool {
        let Some(rx) = self.pending_remote_sessions.as_ref() else {
            return false;
        };
        match rx.try_recv() {
            Ok(sessions) => {
                // #559: merge rather than replace so that an optimistic entry
                // (assignment_id starts with "pending-") added on a fresh
                // launch isn't clobbered by a discovery sweep that ran before
                // the new session was discoverable.  Keep any pending entry
                // whose (repo_name, issue_number) pair is NOT already covered
                // by the real discovery result; drop it once the real session
                // appears (to avoid stale phantom entries accumulating).
                let covered: std::collections::HashSet<(Option<String>, Option<u64>)> = sessions
                    .iter()
                    .map(|s| (s.repo_name.clone(), s.issue_number))
                    .collect();
                let surviving_pending: Vec<LiveTmuxSession> = self
                    .live_tmux_sessions
                    .drain(..)
                    .filter(|s| {
                        s.assignment_id.starts_with("pending-")
                            && !covered.contains(&(s.repo_name.clone(), s.issue_number))
                    })
                    .collect();
                self.live_tmux_sessions = surviving_pending;
                self.live_tmux_sessions.extend(sessions);
                self.pending_remote_sessions = None;
                true
            }
            Err(std::sync::mpsc::TryRecvError::Empty) => false,
            Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                self.pending_remote_sessions = None;
                false
            }
        }
    }

    /// Pipeline panel detail-side: list-style fallback when no PipelineView
    /// can be drawn yet (no issue selected / still loading).
    fn pipeline_placeholder_list(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();
        if self.pending_data.is_some() && self.pipeline_issues.is_empty() {
            items.push(kv_item(
                "",
                "  Loading tracked issues…",
                Some(Color::rgb(180, 180, 100)),
            ));
        } else if self.pipeline_issues.is_empty() {
            let labels = self.data.pipeline_tracked_labels.join(", ");
            items.push(kv_item(
                "",
                &format!(
                    "  No issues found with label(s): {}",
                    if labels.is_empty() {
                        "(none)".into()
                    } else {
                        labels
                    }
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
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    fn board_detail_tab_bar(&self) -> TabBar {
        // #316: show an active-dot on the Board Chat tab while a board chat is live.
        let board_chat_live = self.chat_is_board_chat();
        // #675: dot indicator on the Terminal tab when a session exists for the
        // selected board issue.
        let board_terminal_live = self.board_selected_issue().map_or(false, |(repo, num)| {
            // Resolve the repo_slug so we can look up the session key.
            let slug = self
                .data
                .pipeline_repos
                .iter()
                .find(|(name, _)| *name == repo)
                .map(|(_, s)| s.as_str())
                .unwrap_or(repo.as_str())
                .to_string();
            self.detail_terminal_sessions.contains_key(&(slug, num))
        });
        TabBar {
            id: WidgetId::new("board-detail-tabs"),
            tabs: vec![
                TabItem {
                    label: " Board ".to_string(),
                    is_active: self.board_detail_tab == BoardDetailTab::Board,
                    is_dirty: false,
                    is_preview: false,
                    is_closable: false,
                },
                TabItem {
                    label: " Issue ".to_string(),
                    is_active: self.board_detail_tab == BoardDetailTab::Issue,
                    is_dirty: false,
                    is_preview: false,
                    is_closable: false,
                },
                TabItem {
                    // #316: dot indicator when a board chat is live so the
                    // tab is discoverable without forcing the user back to it.
                    // #675: renamed "Chat" → "Board Chat" to distinguish it
                    // from the new per-issue Terminal tab below.
                    label: if board_chat_live {
                        " Board Chat ● ".to_string()
                    } else {
                        " Board Chat ".to_string()
                    },
                    is_active: self.board_detail_tab == BoardDetailTab::Chat,
                    is_dirty: false,
                    is_preview: false,
                    is_closable: false,
                },
                TabItem {
                    // #675: per-issue interactive terminal.  Dot when a session
                    // is live for the selected issue.
                    label: if board_terminal_live {
                        " Terminal ● ".to_string()
                    } else {
                        " Terminal ".to_string()
                    },
                    is_active: self.board_detail_tab == BoardDetailTab::Terminal,
                    is_dirty: false,
                    is_preview: false,
                    is_closable: false,
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
        // #669: use the panel width stashed at draw time for word-wrapping.
        let wrap_width = self.last_issue_panel_cols.get().max(40);
        let repo = self.board_active_repo().map(str::to_string);
        let group = self.board_selected_issue_group().cloned();
        let (Some(repo), Some(g)) = (repo, group) else {
            return issue_body_list(None, self.detail_scroll, "board-issue-body", wrap_width);
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
                Some((
                    oi.number,
                    oi.title.as_str(),
                    oi.body.as_str(),
                    &oi.labels[..],
                )),
                self.detail_scroll,
                "board-issue-body",
                wrap_width,
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
                wrap_width,
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
                self.pending_issue_fetches
                    .borrow_mut()
                    .insert(key.clone(), rx);
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
                    wrap_width,
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
            wrap_width,
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
                    is_closable: false,
                },
                TabItem {
                    label: " Issue ".to_string(),
                    is_active: self.pipeline_detail_tab == PipelineDetailTab::Issue,
                    is_dirty: false,
                    is_preview: false,
                    is_closable: false,
                },
                TabItem {
                    label: " Stages ".to_string(),
                    is_active: self.pipeline_detail_tab == PipelineDetailTab::Stages,
                    is_dirty: false,
                    is_preview: false,
                    is_closable: false,
                },
                TabItem {
                    label: " Log ".to_string(),
                    is_active: self.pipeline_detail_tab == PipelineDetailTab::Log,
                    is_dirty: false,
                    is_preview: false,
                    is_closable: false,
                },
                // #558: session-history summary tab.
                TabItem {
                    label: " Summary ".to_string(),
                    is_active: self.pipeline_detail_tab == PipelineDetailTab::Summary,
                    is_dirty: false,
                    is_preview: false,
                    is_closable: false,
                },
                TabItem {
                    // #264: an indicator dot when a refinement worker is
                    // actively talking on this issue makes the tab
                    // discoverable without forcing the user back onto it.
                    label: if self.has_active_refinement_for_selected_issue() {
                        " Refinement ● ".to_string()
                    } else {
                        " Refinement ".to_string()
                    },
                    is_active: self.pipeline_detail_tab == PipelineDetailTab::Refinement,
                    is_dirty: false,
                    is_preview: false,
                    is_closable: false,
                },
                // #440: per-issue interactive shell tab.
                TabItem {
                    label: if self.detail_terminal_focused
                        && self.pipeline_detail_tab == PipelineDetailTab::Terminal
                    {
                        " Terminal ▶ ".to_string()
                    } else {
                        " Terminal ".to_string()
                    },
                    is_active: self.pipeline_detail_tab == PipelineDetailTab::Terminal,
                    is_dirty: false,
                    is_preview: false,
                    is_closable: false,
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
        // #669: use the panel width stashed at draw time for word-wrapping.
        let wrap_width = self.last_issue_panel_cols.get().max(40);
        let issue = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i));
        issue_body_list(
            issue.map(|i| {
                (
                    i.number,
                    i.title.as_str(),
                    i.body.as_str(),
                    &i.all_labels[..],
                )
            }),
            self.pipeline_detail_scroll,
            "pipeline-issue-body",
            wrap_width,
        )
    }

    /// Pipeline tab: meta strip (repo/labels/gates/status) plus
    /// #271 part 2 test guidance (branch / repo path / suggested
    /// commands / persisted Phase 1 build result) when Test is
    /// actionable or has been built.
    ///
    /// Still used by tests; the render path now uses
    /// `pipeline_tab_body_list` which inlines this content alongside the
    /// focused-stage output.
    #[allow(dead_code)]
    fn pipeline_issue_summary(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();
        if let Some(idx) = self.pipeline_sel {
            if let Some(issue) = self.pipeline_issues.get(idx) {
                items.push(kv_item(
                    "Repo",
                    &issue.repo_slug,
                    Some(Color::rgb(160, 160, 180)),
                ));
                if let Some(local) = &issue.coord_repo {
                    items.push(kv_item("Local", local, Some(Color::rgb(140, 200, 140))));
                } else {
                    items.push(kv_item(
                        "Local",
                        "(no coordinator.yml mapping)",
                        Some(Color::rgb(220, 150, 80)),
                    ));
                }
                if !issue.matched_labels.is_empty() {
                    items.push(kv_item(
                        "Labels",
                        &issue.matched_labels.join(", "),
                        Some(Color::rgb(160, 160, 180)),
                    ));
                }
                items.push(kv_item(
                    "Gates",
                    &self.pipeline_stage_names_for_issue(issue).join(" → "),
                    Some(Color::rgb(160, 160, 180)),
                ));
                if let Some((msg, when)) = &self.pipeline_status {
                    if when.elapsed() < Duration::from_secs(8) {
                        items.push(kv_item("", "", None));
                        items.push(kv_item(
                            "",
                            &format!("  {}", msg),
                            Some(Color::rgb(180, 180, 100)),
                        ));
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
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    /// Body list for the **Pipeline** detail tab: issue meta summary
    /// (repo, labels, gates, test guidance) followed immediately by the
    /// focused stage's full content (plan log, worker log, test output,
    /// review verdict + body, merge details).
    ///
    /// Replacing the plain `pipeline_issue_summary` with this combined
    /// list means the user sees the most relevant stage output on the
    /// default tab without switching to Stages.  The scroll offset is
    /// driven by `pipeline_stage_content_scroll` so the wheel and j/k
    /// keys can move through the content as on the Stages tab.
    fn pipeline_tab_body_list(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();
        let issue = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))
            .cloned();

        // ── Meta summary (repo / labels / gates / status) ────────────
        if let Some(ref issue) = issue {
            items.push(kv_item(
                "Repo",
                &issue.repo_slug,
                Some(Color::rgb(160, 160, 180)),
            ));
            if let Some(local) = &issue.coord_repo {
                items.push(kv_item("Local", local, Some(Color::rgb(140, 200, 140))));
            } else {
                items.push(kv_item(
                    "Local",
                    "(no coordinator.yml mapping)",
                    Some(Color::rgb(220, 150, 80)),
                ));
            }
            if !issue.matched_labels.is_empty() {
                items.push(kv_item(
                    "Labels",
                    &issue.matched_labels.join(", "),
                    Some(Color::rgb(160, 160, 180)),
                ));
            }
            items.push(kv_item(
                "Gates",
                &self.pipeline_stage_names_for_issue(issue).join(" → "),
                Some(Color::rgb(160, 160, 180)),
            ));
            // #546: per-issue cost rollup — sum of metered (claude -p) cost_usd
            // across all stage iterations (work + review + fix + smoke + plan).
            // Interactive (Max subscription) assignments show cost_usd=NULL and
            // are excluded; they appear as individual "Max" rows in the stages.
            if let Some(total_cost) = self.issue_total_cost(issue) {
                let tok = self.issue_total_tokens(issue);
                let cost_str = if tok > 0 {
                    format!("{}  ({} tokens)", format_cost_usd(total_cost), fmt_tokens(tok))
                } else {
                    format_cost_usd(total_cost)
                };
                items.push(kv_item(
                    "Cost (Σ)",
                    &cost_str,
                    Some(Color::rgb(160, 220, 160)),
                ));
            }
            if let Some((msg, when)) = &self.pipeline_status {
                if when.elapsed() < Duration::from_secs(8) {
                    items.push(kv_item("", "", None));
                    items.push(kv_item(
                        "",
                        &format!("  {}", msg),
                        Some(Color::rgb(180, 180, 100)),
                    ));
                }
            }
            self.append_test_guidance_rows(&mut items, issue);
        }

        // ── Focused-stage content ─────────────────────────────────────
        if let Some(ref issue) = issue {
            let stage_names = self.pipeline_stage_names_for_issue(issue);
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
                    "   ([/] previous · [/] next · click a stage box above to switch)",
                    Some(Color::rgb(140, 140, 160)),
                ));
                items.push(kv_item("", "", None));
                let content_rows = self.stage_content_for(issue, name);
                if content_rows.is_empty() {
                    items.push(kv_item(
                        "",
                        "   (no content available for this stage yet)",
                        Some(Color::rgb(140, 140, 160)),
                    ));
                } else {
                    items.extend(content_rows);
                }
            }
        }

        ListView {
            id: WidgetId::new("pipeline-tab-body"),
            title: None,
            items,
            selected_idx: 0,
            scroll_offset: self.pipeline_stage_content_scroll,
            has_focus: false,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    /// #271 part 2: append a "Test guidance" block — branch, local
    /// path, last-build outcome (persisted), suggested next commands —
    /// when the user is looking at an issue whose Test stage is in
    /// play (actionable or recently built).
    fn append_test_guidance_rows(&self, items: &mut Vec<ListItem>, issue: &PipelineIssue) {
        // Find the latest Work assignment for this issue (the build
        // hangs off its branch).
        let work = self.assignments_for_stage(issue, "work");
        let latest = work.iter().max_by(|a, b| {
            a.dispatched_at
                .partial_cmp(&b.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        let Some(latest) = latest else {
            return;
        };
        // Show this block ONLY when the Test stage is in play (Active
        // or Pending with Work done).  Skip for issues that aren't at
        // the test step yet.
        let test_status = self.test_stage_status_for(issue);
        let actionable = self.test_gate_actionable();
        let has_build_record = self.last_test_builds.contains_key(&latest.id);
        let has_pull_record = self.last_artifact_pulls.contains_key(&latest.id);
        let in_flight = self.test_build_in_flight(&latest.id);
        if !actionable && !has_build_record && !has_pull_record && !in_flight {
            return;
        }

        items.push(kv_item("", "", None));
        items.push(kv_item(
            "Test",
            "ready for manual verification",
            Some(Color::rgb(160, 200, 220)),
        ));
        if let Some(branch) = &latest.branch {
            items.push(kv_item("  Branch", branch, Some(Color::rgb(160, 160, 180))));
        }
        // Local repo path — only when we have a coord-repo mapping.
        if let Some(local) = &issue.coord_repo {
            items.push(kv_item(
                "  Path",
                &format!("see coordinator.yml `repos`: {}", local),
                Some(Color::rgb(160, 160, 180)),
            ));
        }
        // #296: run_cmd — show the manual launch command when defined for
        // this repo.  Absent repos (no run_cmd set) silently skip.
        if let Some(local) = &issue.coord_repo {
            if let Some(cmd) = self.data.pipeline_repo_run_cmds.get(local.as_str()) {
                items.push(kv_item("  Run", cmd, Some(Color::rgb(200, 220, 160))));
            }
        }
        // Persistent build status.  Three states surfaced.
        if in_flight {
            // The job is still going; show elapsed.
            if let Some(job) = self.test_build_jobs.get(&latest.id) {
                let elapsed = job.started_at.elapsed().as_secs();
                items.push(kv_item(
                    "  Build",
                    &format!(
                        "running ({elapsed}s elapsed) — log {}",
                        job.log_path.display()
                    ),
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
                        last.duration_secs,
                        ago,
                        branch_note,
                        last.log_path.display()
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
        // #434: Persistent artifact-pull result — survives the 4 s toast.
        if let Some(pull) = self.last_artifact_pulls.get(&latest.id) {
            let ago = pull.finished_at.elapsed().as_secs();
            if pull.exit_code == 0 {
                items.push(kv_item(
                    "  Pull",
                    &format!("✓ → {} ({}s ago)", pull.message, ago),
                    Some(Color::rgb(120, 200, 120)),
                ));
            } else {
                let snippet: String = pull.message.chars().take(80).collect();
                let ellipsis = if pull.message.chars().count() > 80 {
                    "…"
                } else {
                    ""
                };
                items.push(kv_item(
                    "  Pull",
                    &format!(
                        "✗ exit {} ({}s ago): {}{}",
                        pull.exit_code, ago, snippet, ellipsis
                    ),
                    Some(Color::rgb(220, 100, 100)),
                ));
            }
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
                        items.push(kv_item("  PR notes", "", Some(Color::rgb(160, 160, 180))));
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
                            items.push(kv_item("", "    …", Some(Color::rgb(140, 140, 160))));
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
                    let latest_review = pr.reviews.iter().rev().find(|r| {
                        r.state != "PENDING"
                            && (!r.body.is_empty()
                                || r.state == "APPROVED"
                                || r.state == "CHANGES_REQUESTED")
                    });
                    if let Some(rev) = latest_review {
                        let (state_label, state_color) = match rev.state.as_str() {
                            "APPROVED" => ("✓ Approved", Color::rgb(120, 200, 120)),
                            "CHANGES_REQUESTED" => {
                                ("✗ Changes Requested", Color::rgb(220, 100, 100))
                            }
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
                            items.push(kv_item("", "    …", Some(Color::rgb(140, 140, 160))));
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

        // #336/#433: Artifact badge — show when the manifest is cached and
        // non-empty for this branch.  When the fetch has completed but no
        // artifacts are available, surface the specific reason rather than
        // silently hiding the badge (intermittency was invisible before #433).
        if let Some(branch) = &latest.branch {
            let sanitized = sanitize_branch(branch);
            let key = (latest.repo.clone(), sanitized.clone());
            match self.artifact_cache.get(&key) {
                Some(entry) => {
                    if let Some(manifest) = &entry.manifest {
                        // ── Artifact stash found — show the download badge ────
                        let file_count = manifest.files.len();
                        let total_mb = manifest.total_bytes as f64 / 1_048_576.0;
                        // Warn when the stash was built by a different
                        // assignment — e.g. the branch was re-pushed and a
                        // newer worker ran.
                        let built_by_note = if manifest.built_by_assignment_id.as_deref()
                            != Some(latest.id.as_str())
                        {
                            if let Some(id) = &manifest.built_by_assignment_id {
                                let id_short: String = id.chars().take(8).collect();
                                format!(" [built by {}]", id_short)
                            } else {
                                String::new()
                            }
                        } else {
                            String::new()
                        };
                        items.push(kv_item("", "", None));
                        items.push(kv_item(
                            "  Artifacts",
                            &format!(
                                "📦 {} file{}, {:.1} MB on {}{} — press a to pull",
                                file_count,
                                if file_count == 1 { "" } else { "s" },
                                total_mb,
                                latest.machine,
                                built_by_note,
                            ),
                            Some(Color::rgb(200, 180, 100)),
                        ));
                    } else {
                        // ── Fetch completed but no artifacts available ────────
                        // Surface why, so intermittent absences are diagnosable.
                        let reason = match &entry.absence_reason {
                            Some(ArtifactAbsence::NotStashed)
                            | Some(ArtifactAbsence::ManifestEmpty) => {
                                if issue_produces_build_artifact(&latest.repo, &issue.title) {
                                    format!("no binary built on {} — a: how to test", latest.machine)
                                } else {
                                    "CLI change — no binary; a: how to test".to_string()
                                }
                            }
                            Some(ArtifactAbsence::AgentUnreachable(e)) => {
                                let msg: String = e.chars().take(80).collect();
                                let ellipsis = if e.chars().count() > 80 { "…" } else { "" };
                                format!("agent unreachable: {}{}", msg, ellipsis)
                            }
                            None => "(fetch result unknown)".to_string(),
                        };
                        items.push(kv_item("", "", None));
                        items.push(kv_item(
                            "  Artifacts",
                            &format!("[a] unavailable — {}", reason),
                            Some(Color::rgb(160, 140, 100)),
                        ));
                    }
                }
                None => {
                    // No cache entry yet — fetch is in-flight (triggered by
                    // the tick handler as soon as the Pipeline view is active).
                    items.push(kv_item("", "", None));
                    items.push(kv_item(
                        "  Artifacts",
                        "(checking agent…)",
                        Some(Color::rgb(140, 140, 160)),
                    ));
                }
            }
        }

        // Suggested next steps.
        let test_label = match test_status {
            StageStatus::Failed => {
                "previously failed — press R to re-dispatch Work, or P/F/S to re-record"
            }
            _ => "press P=pass, F=fail, r=report+fix, S=skip after manual verification",
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
            // Plan: prefer the structured plan cached in the plans table
            // (parsed by `coord notify`); fall back to log tail if no row.
            // Without this the panel dumped raw stream-json events,
            // unreadable to humans.
            "plan" => self.stage_content_plan(issue),
            // Work: read the latest assignment's log tail.
            "work" => self.stage_content_assignment_log(issue, stage_name),
            _ => Vec::new(),
        }
    }

    /// Render the structured plan for the selected pipeline issue.
    /// Pulls from `BoardData.plans` (populated by `coord notify` parsing
    /// the plan worker's log).  When no plan row exists (notify hasn't
    /// run yet, or the worker exited without a structured plan), falls
    /// back to the log-tail view so the user sees something.
    fn stage_content_plan(&self, issue: &PipelineIssue) -> Vec<ListItem> {
        let local_repo = issue.coord_repo.as_deref();
        let plan_assignment = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == r,
                None => true,
            })
            .filter(|a| a.assignment_type.as_deref() == Some("plan"))
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            });
        let Some(a) = plan_assignment else {
            return vec![kv_item(
                "",
                "   (plan assignment not found in board — press S to sync, or run `coord notify`)",
                Some(Color::rgb(160, 160, 180)),
            )];
        };
        let Some(plan) = self.data.plans.get(&a.id) else {
            // No structured plan cached — fall back to log tail with a hint.
            let mut rows = vec![kv_item(
                "",
                "   (plan not yet parsed — run `coord notify` to refresh)",
                Some(Color::rgb(160, 160, 180)),
            )];
            rows.push(kv_item("", "", None));
            rows.extend(self.stage_content_assignment_log(issue, "plan"));
            return rows;
        };

        let label_color = Color::rgb(180, 200, 240);
        let body_color = Color::rgb(220, 220, 220);
        let mut rows: Vec<ListItem> = Vec::new();

        if !plan.plan.is_empty() {
            rows.push(kv_item("", " Summary", Some(label_color)));
            for line in plan.plan.lines() {
                rows.push(kv_item("", &format!("   {}", line), Some(body_color)));
            }
            rows.push(kv_item("", "", None));
        }

        if !plan.files_modify.is_empty() {
            rows.push(kv_item("", " Files to modify", Some(label_color)));
            for f in &plan.files_modify {
                rows.push(kv_item("", &format!("   - {}", f), Some(body_color)));
            }
            rows.push(kv_item("", "", None));
        }

        if !plan.approach.is_empty() {
            rows.push(kv_item("", " Approach", Some(label_color)));
            for line in plan.approach.lines() {
                rows.push(kv_item("", &format!("   {}", line), Some(body_color)));
            }
            rows.push(kv_item("", "", None));
        }

        if !plan.risks.is_empty() {
            rows.push(kv_item("", " Risks", Some(label_color)));
            for line in plan.risks.lines() {
                rows.push(kv_item("", &format!("   {}", line), Some(body_color)));
            }
            rows.push(kv_item("", "", None));
        }

        if !plan.estimate.is_empty() {
            rows.push(kv_item("", " Estimate", Some(label_color)));
            rows.push(kv_item(
                "",
                &format!("   {}", plan.estimate),
                Some(body_color),
            ));
            rows.push(kv_item("", "", None));
        }

        match &plan.smoke_tests {
            Some(bullets) if bullets.is_empty() => {
                rows.push(kv_item("", " Smoke tests", Some(label_color)));
                rows.push(kv_item(
                    "",
                    "   (none — change is internal)",
                    Some(Color::rgb(160, 160, 180)),
                ));
                rows.push(kv_item("", "", None));
            }
            Some(bullets) => {
                rows.push(kv_item("", " Smoke tests", Some(label_color)));
                for b in bullets {
                    rows.push(kv_item("", &format!("   - {}", b), Some(body_color)));
                }
                rows.push(kv_item("", "", None));
            }
            None => {
                // Plan worker predates the SMOKE_TESTS-in-plan prompt
                // (or just forgot).  Show a muted line so the user knows
                // it's missing but doesn't have to dig.
                rows.push(kv_item("", " Smoke tests", Some(label_color)));
                rows.push(kv_item(
                    "",
                    "   (no SMOKE_TESTS block in plan — author manually)",
                    Some(Color::rgb(160, 160, 180)),
                ));
                rows.push(kv_item("", "", None));
            }
        }

        rows
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
        let Some(review) = review else {
            return Vec::new();
        };
        // Findings JSON was loaded with the board (no per-render DB
        // query).  When None: for an automated review, notify hasn't parsed
        // it yet — run `coord notify` or `coord bounce`.  For a human-
        // attended review with request-changes (#587), the findings were
        // never written; the rework dialog will capture them when the fix
        // is started.
        let Some(raw) = review.review_findings.as_deref() else {
            if review.review_verdict.as_deref() == Some("request-changes") {
                return vec![
                    kv_item(
                        "",
                        "   ⚠ No findings captured for this review.",
                        Some(Color::rgb(220, 180, 80)),
                    ),
                    kv_item(
                        "",
                        "   Use 'Start Fix' — the dialog will ask you to enter them.",
                        Some(Color::rgb(180, 180, 180)),
                    ),
                ];
            }
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

    /// Test stage content — #349 plan (if available) + the cached build log.
    ///
    /// Shows the AI-generated smoke test plan as a "SMOKE TEST PLAN" system
    /// block at the top, with numbered steps that the user can run via keys
    /// 1–8.  Below that, the cached `coord test` build log is rendered as
    /// before.
    fn stage_content_test(&self, issue: &PipelineIssue) -> Vec<ListItem> {
        // Find the latest work assignment.
        let local_repo = issue.coord_repo.as_deref();
        let work_assignment = self
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
            });
        let Some(work) = work_assignment else {
            return Vec::new();
        };
        let work_id = work.id.clone();
        let mut rows: Vec<ListItem> = Vec::new();

        // ── #349: Smoke test plan section ────────────────────────────────────
        let header_color = Color::rgb(200, 200, 240);
        let step_color = Color::rgb(220, 220, 220);
        let pending_color = Color::rgb(220, 180, 100);
        let ok_color = Color::rgb(120, 200, 120);
        let fail_color = Color::rgb(220, 100, 100);
        let dim_color = Color::rgb(140, 140, 160);

        match &work.test_plan {
            Some(steps) => {
                rows.push(kv_item("", "── SMOKE TEST PLAN ──", Some(header_color)));
                rows.push(kv_item(
                    "",
                    "   Press 1–9 to run a step.  [a] to pull artifacts.  \
                     Verify steps: press key to mark ✓.",
                    Some(dim_color),
                ));
                rows.push(kv_item("", "", None));

                // Assign number keys only to non-pull steps (1–9).
                // Pull steps use the [a] keybind and display [a] as their hint.
                let mut run_key: u8 = 0;
                for (i, step) in steps.iter().enumerate() {
                    // Determine the display key for this step.
                    let is_pull = step.kind == "pull";
                    let key_hint: String = if is_pull {
                        "[a]".to_string()
                    } else {
                        run_key += 1;
                        if run_key > 9 {
                            // More than 9 runnable steps — stop rendering to
                            // avoid implying a key binding that doesn't exist.
                            break;
                        }
                        format!("[{}]", run_key)
                    };

                    // Determine status indicator for this step.
                    let key = (work_id.clone(), i);
                    let status_str = if self.test_step_jobs.contains_key(&key) {
                        "⏳ running…".to_string()
                    } else if let Some(&exit) = self.test_step_results.get(&key) {
                        if exit == 0 {
                            "✓".to_string()
                        } else {
                            format!("✗ (exit {})", exit)
                        }
                    } else {
                        String::new()
                    };
                    let status_color = if self.test_step_jobs.contains_key(&key) {
                        pending_color
                    } else if let Some(&exit) = self.test_step_results.get(&key) {
                        if exit == 0 {
                            ok_color
                        } else {
                            fail_color
                        }
                    } else {
                        step_color
                    };

                    // Build the step description line.
                    let desc = match step.kind.as_str() {
                        "pull" => {
                            let label = step.label.as_deref().unwrap_or("");
                            let cmd = step.cmd.as_deref().unwrap_or("(no cmd)");
                            if label.is_empty() {
                                format!("{} pull: {}", key_hint, cmd)
                            } else {
                                format!("{} pull {}: {}", key_hint, label, cmd)
                            }
                        }
                        "verify" => {
                            let check = step.check.as_deref().unwrap_or("(no check)");
                            format!("{} (verify) {}", key_hint, check)
                        }
                        _ => {
                            // "run" and unknown kinds.
                            let cmd = step.cmd.as_deref().unwrap_or("(no cmd)");
                            format!("{} {}", key_hint, cmd)
                        }
                    };
                    let desc_capped: String = desc.chars().take(160).collect();
                    let display = if status_str.is_empty() {
                        desc_capped
                    } else {
                        format!("{desc_capped}  {status_str}")
                    };
                    rows.push(kv_item("", &format!("   {display}"), Some(status_color)));

                    // Display captured output lines below the step row.
                    if let Some(output) = self.test_step_output.get(&key) {
                        for line in output.lines().take(50) {
                            let trimmed: String = line.chars().take(160).collect();
                            rows.push(kv_item("", &format!("     {trimmed}"), Some(dim_color)));
                        }
                    }
                }
                rows.push(kv_item("", "", None));
            }
            None => {
                // Plan not yet generated — "Preparing plan…" placeholder.
                // `maybe_spawn_test_plan` (called each tick) will spawn
                // `coord test-plan` the next time it runs.
                rows.push(kv_item("", "── SMOKE TEST PLAN ──", Some(header_color)));
                if self.test_plan_pending.contains(&work_id) {
                    rows.push(kv_item(
                        "",
                        "   Preparing plan… (running `coord test-plan`)",
                        Some(pending_color),
                    ));
                } else {
                    rows.push(kv_item("", "   Preparing plan…", Some(pending_color)));
                }
                rows.push(kv_item("", "", None));
            }
        }

        // ── #434: Artifact-pull result ───────────────────────────────────────
        if let Some(pull) = self.last_artifact_pulls.get(&work_id) {
            let ago = pull.finished_at.elapsed().as_secs();
            if pull.exit_code == 0 {
                rows.push(kv_item(
                    "Last pull",
                    &format!("✓ → {} ({}s ago)", pull.message, ago),
                    Some(ok_color),
                ));
            } else {
                let snippet: String = pull.message.chars().take(80).collect();
                let ellipsis = if pull.message.chars().count() > 80 {
                    "…"
                } else {
                    ""
                };
                rows.push(kv_item(
                    "Last pull",
                    &format!(
                        "✗ exit {} ({}s ago): {}{}",
                        pull.exit_code, ago, snippet, ellipsis
                    ),
                    Some(fail_color),
                ));
            }
        }

        // ── Build log section (unchanged from prior implementation) ─────────
        let Some(build) = self.last_test_builds.get(&work_id) else {
            rows.push(kv_item(
                "",
                "   (no build recorded — press B to run `coord test`)",
                Some(dim_color),
            ));
            return rows;
        };
        let (status_label, status_color) = if build.exit_code == 0 {
            ("✓ succeeded", ok_color)
        } else {
            ("✗ failed", fail_color)
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
            .find(|m| m.issue_number == Some(issue.number) && m.repo_github == issue.repo_slug);
        let Some(entry) = entry else {
            return Vec::new();
        };
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
    fn stage_content_assignment_log(&self, issue: &PipelineIssue, stage: &str) -> Vec<ListItem> {
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
        let Some(a) = assignment else {
            return Vec::new();
        };
        let log_path = std::path::PathBuf::from(std::env::var("HOME").unwrap_or_default())
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
                    lines.len(),
                    a.id,
                ),
                Some(Color::rgb(140, 140, 160)),
            ));
            rows.push(kv_item("", "", None));
        }
        for line in &lines[tail_start..] {
            // #302: don't hard-clip at 180 — the Log tab is horizontally
            // scrollable. Keep a generous cap so a pathological single-line
            // blob can't blow up the row, but let normal lines through whole.
            let trimmed: String = line.chars().take(4000).collect();
            rows.push(kv_item("", &format!("   {trimmed}"), None));
        }
        rows
    }

    /// Log tab: show the worker log for the selected pipeline issue.
    ///
    /// Prefers the live SSE stream when open for this issue's assignment
    /// (avoids the polling "Loading log…" flicker every cache-TTL seconds).
    /// Falls back to `get_activity_log` (local file or remote HTTP cache)
    /// when no SSE is active.
    ///
    /// Content items (the expensive parse) are cached in `log_items_cache`
    /// and rebuilt only when the assignment, line count, or wrap width
    /// changes (#399 scroll-perf).
    fn pipeline_log_list(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();
        let issue = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i));
        if let Some(issue) = issue {
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
                        .max_by(|a, b| {
                            a.dispatched_at
                                .partial_cmp(&b.dispatched_at)
                                .unwrap_or(std::cmp::Ordering::Equal)
                        })
                });
            if let Some(a) = assignment {
                // Session elapsed header — always recomputed (time advances every
                // second even when no new log lines arrive).
                let now_secs = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs_f64())
                    .unwrap_or(0.0);
                let elapsed_header = match (a.dispatched_at, a.finished_at) {
                    (Some(start), Some(end)) => {
                        let secs = (end - start).max(0.0) as u64;
                        format!(
                            "  {} · {} · elapsed {}",
                            a.assignment_type.as_deref().unwrap_or("work"),
                            a.machine,
                            fmt_elapsed_mmss(secs)
                        )
                    }
                    (Some(start), None) => {
                        let secs = (now_secs - start).max(0.0) as u64;
                        format!(
                            "  {} · {} · running {}",
                            a.assignment_type.as_deref().unwrap_or("work"),
                            a.machine,
                            fmt_elapsed_mmss(secs)
                        )
                    }
                    _ => format!(
                        "  {} · {}",
                        a.assignment_type.as_deref().unwrap_or("work"),
                        a.machine
                    ),
                };
                items.push(kv_item(
                    "",
                    &elapsed_header,
                    Some(Color::rgb(120, 120, 140)),
                ));
                items.push(kv_item("", "", None));

                // #385: readable wrapped rendering.  Panel width is set by
                // the render path via `last_log_panel_cols` just before calling
                // this method — TUI backends store columns directly; GTK stores
                // pixels (large values mean wrapping won't fire, which is fine
                // since GTK handles layout internally).
                let wrap_width = self.last_log_panel_cols.get().max(40);

                // Use SSE from the watch pool if a stream exists for this
                // assignment (focused or background) — avoids the
                // HTTP-cache-TTL "Loading log…" flicker.
                if let Some(ctx) = self.watch_pool.get(&a.id) {
                    let sse = &ctx.sse;
                    if sse.lines.is_empty() && !sse.done {
                        items.push(kv_item(
                            "",
                            "  Connecting to log stream…",
                            Some(Color::rgb(140, 140, 140)),
                        ));
                    } else {
                        // #399/#787 scroll-perf: 3-way cache decision.
                        //   exact hit  → extend items from cache (zero parse).
                        //   can extend → parse only new lines, append (O(new)).
                        //   full build → parse all lines from scratch (O(total)).
                        let line_count = sse.lines.len();
                        // Determine the cache status in a scoped borrow so we
                        // can take `borrow_mut` below without a conflict.
                        enum CacheStatus { ExactHit, CanExtend(usize), FullBuild }
                        let status = {
                            let cache = self.log_items_cache.borrow();
                            match cache.as_ref() {
                                Some(c)
                                    if c.assignment_id == a.id && c.wrap_width == wrap_width =>
                                {
                                    if c.line_count == line_count {
                                        CacheStatus::ExactHit
                                    } else if c.line_count < line_count {
                                        CacheStatus::CanExtend(c.line_count)
                                    } else {
                                        // line_count shrank — defensive full rebuild.
                                        CacheStatus::FullBuild
                                    }
                                }
                                _ => CacheStatus::FullBuild,
                            }
                        };
                        match status {
                            CacheStatus::ExactHit => {
                                let cache = self.log_items_cache.borrow();
                                items.extend(cache.as_ref().unwrap().items.iter().cloned());
                            }
                            CacheStatus::CanExtend(old_count) => {
                                // Parse only the new suffix, append to cached items.
                                let cached = {
                                    let mut cache = self.log_items_cache.borrow_mut();
                                    let c = cache.as_mut().unwrap();
                                    let new_items = parse_sse_log_more(
                                        &sse.lines[old_count..],
                                        &sse.line_times[old_count..],
                                        wrap_width,
                                        &mut c.parse_state,
                                    );
                                    c.items.extend(new_items);
                                    c.line_count = line_count;
                                    c.items.clone()
                                };
                                items.extend(cached);
                            }
                            CacheStatus::FullBuild => {
                                let mut state = LogParseState::default();
                                let content_items = parse_sse_log_more(
                                    &sse.lines,
                                    &sse.line_times,
                                    wrap_width,
                                    &mut state,
                                );
                                *self.log_items_cache.borrow_mut() = Some(LogItemsCache {
                                    assignment_id: a.id.clone(),
                                    line_count,
                                    wrap_width,
                                    items: content_items.clone(),
                                    parse_state: state,
                                });
                                items.extend(content_items);
                            }
                        }
                    }
                    if sse.done {
                        items.push(kv_item(
                            "",
                            "  ── stream ended ──",
                            Some(Color::rgb(90, 90, 90)),
                        ));
                    }
                } else {
                    // For local logs, apply readable formatting directly.
                    // For remote/cached logs, fall back to get_activity_log.
                    let log_path = coord_dir().join("logs").join(format!("{}.log", a.id));
                    if let Ok(content) = std::fs::read_to_string(&log_path) {
                        // #399 scroll-perf: cache local-file parse by byte length.
                        let line_count = content.len();
                        let cache_valid = {
                            let cache = self.log_items_cache.borrow();
                            cache.as_ref().map_or(false, |c| {
                                c.assignment_id == a.id
                                    && c.line_count == line_count
                                    && c.wrap_width == wrap_width
                            })
                        };
                        if cache_valid {
                            let cache = self.log_items_cache.borrow();
                            items.extend(cache.as_ref().unwrap().items.iter().cloned());
                        } else {
                            let content_items = parse_log_content_readable(&content, wrap_width);
                            *self.log_items_cache.borrow_mut() = Some(LogItemsCache {
                                assignment_id: a.id.clone(),
                                line_count,
                                wrap_width,
                                items: content_items.clone(),
                                // File-based path uses exact-match caching; parse_state is
                                // unused here (file content is not parsed incrementally).
                                parse_state: LogParseState::default(),
                            });
                            items.extend(content_items);
                        }
                    } else {
                        items.extend(self.get_activity_log(&a.id, &a.machine));
                    }
                }
            } else {
                items.push(kv_item(
                    "",
                    "  (no assignment log available)",
                    Some(Color::rgb(100, 100, 100)),
                ));
            }
        } else {
            items.push(kv_item(
                "",
                "  (select an issue to view its log)",
                Some(Color::rgb(100, 100, 100)),
            ));
        }
        // Sticky-to-bottom: usize::MAX is the sentinel for "follow tail".
        // Compute the real offset here so draw_list gets a clamped value.
        let visible_rows = self.last_main_visible_rows.get().max(1);
        let scroll = if self.pipeline_detail_scroll == usize::MAX {
            items.len().saturating_sub(visible_rows)
        } else {
            self.pipeline_detail_scroll
        };
        // #302: measure the widest row so the rasteriser knows when content
        // overflows and should paint a horizontal scrollbar. Width = the 3-char
        // "   " indent the rows carry + the item's visible text width.
        let max_content_width = items.iter().map(|it| 3 + it.text.visible_width()).max();
        // Clamp the horizontal offset so it can't scroll past the content.
        let h_scroll = match max_content_width {
            Some(w) => self.pipeline_log_hscroll.min(w.saturating_sub(1)),
            None => 0,
        };
        ListView {
            id: WidgetId::new("pipeline-log"),
            title: None,
            items,
            selected_idx: 0,
            scroll_offset: scroll,
            has_focus: false,
            bordered: false,
            h_scroll,
            max_content_width,
            show_v_scrollbar: false,
        }
    }

    /// Count `[assistant]` turns in the local log for an assignment.
    /// Returns 0 when the log is not cached locally.
    fn turn_count_from_log(&self, assignment_id: &str) -> usize {
        let path = coord_dir()
            .join("logs")
            .join(format!("{}.log", assignment_id));
        let Ok(content) = std::fs::read_to_string(&path) else {
            return 0;
        };
        content
            .lines()
            .filter(|l| json_str(l, "type").as_deref() == Some("assistant"))
            .count()
    }

    fn pipeline_stages_list(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();
        let issue = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))
            .cloned();
        let Some(issue) = issue else {
            items.push(kv_item(
                "",
                "(no issue selected)",
                Some(Color::rgb(140, 140, 140)),
            ));
            return ListView {
                id: WidgetId::new("pipeline-stages"),
                title: None,
                items,
                selected_idx: 0,
                scroll_offset: 0,
                has_focus: false,
                bordered: false,
                h_scroll: 0,
                max_content_width: None,
                show_v_scrollbar: false,
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
                h_scroll: 0,
                max_content_width: None,
                show_v_scrollbar: false,
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

            self.append_assignment_stage_rows(&mut items, &issue, name);
            items.push(kv_item("", "", None));
        }

        // #stage-content: per-stage content panel.  When a stage is
        // focused ([/] keys, or a click on the stage box on the Pipeline
        // tab), render its associated output at the bottom of the list.
        // Plan → planner agent output; Work → worker log tail;
        // Test → build output; Review → cached review findings.
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
                "   ([/] previous · [/] next stage)",
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
            // No stage focused — auto-focus should handle this, but
            // show a hint for issues with no stages or no content yet.
            items.push(kv_item(
                "",
                " Tip: press [ / ] to view each stage's output here.",
                Some(Color::rgb(140, 140, 160)),
            ));
        }
        ListView {
            id: WidgetId::new("pipeline-stages"),
            title: None,
            items,
            selected_idx: 0,
            // Stage content (esp. a rendered plan) can overflow the panel.
            // Honour the scroll offset driven by the scroll wheel / keys.
            scroll_offset: self.pipeline_stage_content_scroll,
            has_focus: false,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
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
            items.push(kv_item(
                "",
                "    (not started)",
                Some(Color::rgb(140, 140, 140)),
            ));
            return;
        }
        for a in matching.iter() {
            let id_short: String = a.id.chars().take(8).collect();
            items.push(kv_item(
                "Assignment",
                &id_short,
                Some(Color::rgb(160, 200, 220)),
            ));
            items.push(kv_item(
                "Machine",
                &a.machine,
                Some(Color::rgb(210, 210, 210)),
            ));
            let status_color = match a.status.as_str() {
                "running" => Color::rgb(220, 180, 100),
                "done" => Color::rgb(120, 200, 120),
                "failed" => Color::rgb(220, 70, 70),
                _ => Color::rgb(180, 180, 180),
            };
            items.push(kv_item("Status", &a.status, Some(status_color)));
            // #618: when an interactive session fails at launch (e.g. branch
            // already checked out) no log file is produced.  Surface the
            // reason string recorded at failure time so the operator can see
            // WHY the box is red without having to run `coord diagnose`.
            if a.status == "failed" {
                if let Some(reason) = &a.failure_reason {
                    items.push(kv_item(
                        "Failure",
                        reason,
                        Some(Color::rgb(220, 100, 100)),
                    ));
                }
            }
            if let Some(branch) = &a.branch {
                items.push(kv_item("Branch", branch, Some(Color::rgb(200, 200, 200))));
            }
            if let Some(model) = &a.model {
                items.push(kv_item("Model", model, Some(Color::rgb(180, 180, 180))));
            }
            if let Some(t) = a.dispatched_at {
                items.push(kv_item(
                    "Dispatched",
                    &format_unix_time(t),
                    Some(Color::rgb(180, 180, 180)),
                ));
            }
            if let Some(t) = a.finished_at {
                items.push(kv_item(
                    "Finished",
                    &format_unix_time(t),
                    Some(Color::rgb(180, 180, 180)),
                ));
            }
            if let Some(ec) = a.exit_code {
                let ec_color = if ec == 0 {
                    Color::rgb(120, 200, 120)
                } else {
                    Color::rgb(220, 70, 70)
                };
                items.push(kv_item("Exit code", &ec.to_string(), Some(ec_color)));
            }
            // #208/#546: surface captured worker cost + token counts so the
            // user can spot unusually expensive runs without leaving the TUI.
            // Token counts come from the same stream-json parse as cost_usd.
            // Interactive (Max subscription) workers have cost_usd=None and
            // token counts of 0 — we show "Max (subscription)" for those.
            let tok_total = a.input_tokens + a.output_tokens
                + a.cache_creation_tokens + a.cache_read_tokens;
            let has_tokens = tok_total > 0;
            if let Some(cost) = a.cost_usd {
                let tok_suffix = if has_tokens {
                    // Show input·output with a cache note when cache was used.
                    let cache = a.cache_creation_tokens + a.cache_read_tokens;
                    if cache > 0 {
                        format!(
                            "  ({}in · {}out · {}cache)",
                            fmt_tokens(a.input_tokens),
                            fmt_tokens(a.output_tokens),
                            fmt_tokens(cache),
                        )
                    } else {
                        format!(
                            "  ({}in · {}out)",
                            fmt_tokens(a.input_tokens),
                            fmt_tokens(a.output_tokens),
                        )
                    }
                } else {
                    String::new()
                };
                items.push(kv_item(
                    "Cost",
                    &format!("{}{}", format_cost_usd(cost), tok_suffix),
                    Some(Color::rgb(180, 180, 180)),
                ));
            } else if a.is_interactive {
                // Confirmed interactive (Max) session — show billing model.
                // Gate on is_interactive so pre-#208 automated rows (which also have
                // cost_usd=NULL + zero tokens) are not mis-labelled "Max (subscription)".
                // Include token breakdown when the transcript scan succeeded (#546).
                let tok_suffix = if has_tokens {
                    let cache = a.cache_creation_tokens + a.cache_read_tokens;
                    if cache > 0 {
                        format!(
                            "  ({}in · {}out · {}cache)",
                            fmt_tokens(a.input_tokens),
                            fmt_tokens(a.output_tokens),
                            fmt_tokens(cache),
                        )
                    } else {
                        format!(
                            "  ({}in · {}out)",
                            fmt_tokens(a.input_tokens),
                            fmt_tokens(a.output_tokens),
                        )
                    }
                } else {
                    String::new()
                };
                items.push(kv_item(
                    "Cost",
                    &format!("Max (subscription){}", tok_suffix),
                    Some(Color::rgb(140, 140, 160)),
                ));
            } else if has_tokens {
                // Tokens available but cost_usd not yet persisted (uncommon).
                items.push(kv_item(
                    "Tokens",
                    &format!("{}tok", fmt_tokens(tok_total)),
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
                        "request-changes" => ("✗ changes requested", Color::rgb(220, 100, 100)),
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
                modifiers,
                ..
            } => {
                let pos = *position;
                let lh = backend.line_height();
                // #369/#329: an open prompt dialog intercepts all clicks
                // first (highest z-order) — outside → dismiss; on a
                // button → fire action; inside body → swallow.
                if let Some(handled) = self.handle_dialog_click(pos, backend) {
                    return handled;
                }
                // #259: an open context menu intercepts all clicks next
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
                    let main_b = ctx.main_bounds();
                    let char_w = backend.char_width();
                    // #646 focus-follows-click: clicking the terminal content area focuses it.
                    if self.active_view == SidebarView::Terminal && !self.terminal_focused {
                        self.terminal_focused = true;
                    }
                    if self.active_view == SidebarView::Pipeline
                        && self.pipeline_detail_tab == PipelineDetailTab::Terminal
                    {
                        // Only focus when clicking below the tab bar (the terminal content).
                        let tab_h = detail_tab_bar_height(lh);
                        if pos.y - main_b.y >= tab_h && !self.detail_terminal_focused {
                            self.detail_terminal_focused = true;
                        }
                    }
                    // #675: Board Terminal tab — same focus-follows-click as Pipeline Terminal.
                    if self.active_view == SidebarView::Board
                        && self.board_detail_tab == BoardDetailTab::Terminal
                    {
                        let tab_h = detail_tab_bar_height(lh);
                        if pos.y - main_b.y >= tab_h && !self.detail_terminal_focused {
                            self.detail_terminal_focused = true;
                        }
                    }
                    // #464: host-side selection — must check BEFORE the PTY
                    // forwarding path so Shift can override even when the app
                    // has mouse reporting on (e.g. vim visual mode).
                    //
                    // Two cases both route to host selection:
                    //   1. Shift held → always host-select (standard terminal
                    //      override convention; overrides vim/tmux/less).
                    //   2. Mouse reporting OFF → forward_mouse returns false
                    //      anyway; start selection here so we own the drag.
                    //
                    // For case 2 we need to peek at the session's reporting
                    // state without consuming the event — read the flag, then
                    // branch.
                    // Compute cell coordinates once; reuse for both the
                    // reporting-state peek and the host-select branch (avoids
                    // a redundant coordinate translation on every mouse-down).
                    let cr = self.active_terminal_pixel_to_cell(pos, main_b, lh, char_w);
                    let reporting_on = cr.is_some() && {
                        // Only peek when there's actually a session.
                        match self.active_view {
                            SidebarView::Terminal => self
                                .terminal_session
                                .as_ref()
                                .map(|s| s.mouse_reporting_enabled())
                                .unwrap_or(false),
                            SidebarView::Pipeline
                                if self.pipeline_detail_tab
                                    == PipelineDetailTab::Terminal =>
                            {
                                self.selected_issue_key()
                                    .and_then(|k| {
                                        self.detail_terminal_sessions
                                            .get(&k)
                                            .map(|s| s.mouse_reporting_enabled())
                                    })
                                    .unwrap_or(false)
                            }
                            _ => false,
                        }
                    };
                    let force_host_sel = modifiers.shift || !reporting_on;
                    if force_host_sel {
                        if let Some((col, row)) = cr {
                            self.terminal_host_sel_begin(col, row);
                            return true;
                        }
                    }
                    // #454: Forward click to the embedded PTY when mouse
                    // reporting is enabled. Returns true only if the PTY
                    // consumed it (i.e. mouse reporting is on); fall through
                    // to normal TUI click handling otherwise.
                    if self.terminal_mouse_event(
                        TerminalMouseKind::Press,
                        MouseButton::Left,
                        pos,
                        *modifiers,
                        main_b,
                        lh,
                        char_w,
                    ) {
                        // Remember the press so the matching `Release`
                        // fires even if the user drags out of the panel
                        // before releasing (#454 review fix).
                        self.pty_pressed_buttons |= pty_button_bit(MouseButton::Left);
                        return true;
                    }
                    self.mouse_main_click(pos, main_b, lh)
                } else {
                    false
                }
            }
            UiEvent::MouseDown {
                position,
                button: MouseButton::Right,
                modifiers,
                ..
            } => {
                // #259: right-click opens a context menu for the row
                // under the cursor (Board / Pipeline sidebar only for
                // MVP).  We synthesise a left-click first so the row
                // gets focused / selected, then open the menu using the
                // newly-selected row as the target.
                let pos = *position;
                let modifiers = *modifiers;
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
                    // Synthetic-left above already moved the selection to the
                    // row under the cursor, so the shared selection-based
                    // target builder reflects the clicked row.
                    let target = self.context_menu_target_for_selection();
                    if let Some(target) = target {
                        if self.open_context_menu(pos, target) {
                            return true;
                        }
                    }
                } else if ctx.in_main(pos.x, pos.y) {
                    // #454: Forward right-click Press to the embedded PTY when
                    // mouse reporting is enabled.  Without this the PTY would
                    // receive an orphaned Release (from the MouseUp arm) with
                    // no corresponding Press, breaking right-click in apps
                    // such as vim or tmux.
                    let main_b = ctx.main_bounds();
                    let char_w = backend.char_width();
                    let lh = backend.line_height();
                    if self.terminal_mouse_event(
                        TerminalMouseKind::Press,
                        MouseButton::Right,
                        pos,
                        modifiers,
                        main_b,
                        lh,
                        char_w,
                    ) {
                        // Mirror the Left-button path: remember the press
                        // so the matching `Release` fires even if the
                        // user releases outside the panel (#454 fix-2).
                        self.pty_pressed_buttons |= pty_button_bit(MouseButton::Right);
                        return true;
                    }
                }
                false
            }

            UiEvent::Scroll {
                position, delta, ..
            } => {
                let pos = *position;
                let d = *delta;
                let lh = backend.line_height();
                let char_w = backend.char_width();
                if ctx.in_sidebar(pos.x, pos.y) {
                    if let Some(sidebar_b) = ctx.sidebar_bounds() {
                        return self.mouse_sidebar_scroll(event, d, sidebar_b, backend, lh);
                    }
                    false
                } else if ctx.in_main(pos.x, pos.y) {
                    self.mouse_main_scroll(d, pos, ctx.main_bounds(), lh, char_w)
                } else {
                    false
                }
            }

            // #272: drive ToolbarHoverTracker from MouseMoved so the
            // hovered toolbar button gets a background tint without the
            // host having to track button bounds across frames.  A
            // change in the hovered id triggers a redraw.
            UiEvent::MouseMoved { .. } => {
                let (pos, buttons) = if let UiEvent::MouseMoved { position, buttons } = event {
                    (*position, *buttons)
                } else {
                    return false;
                };
                let lh = backend.line_height();
                let mut redraw = false;
                // Forward to the active sidebar so scrollbar drag tracks the cursor.
                // SidebarSystem.handle(MouseMoved) calls drag_to() internally and
                // returns Consumed when scroll state changed — use that to trigger redraw.
                if let Some(sidebar_b) = ctx.sidebar_bounds() {
                    let result = match self.active_view {
                        SidebarView::Board => self.board_sidebar.handle(event, backend, sidebar_b),
                        SidebarView::Pipeline => {
                            self.pipeline_sidebar.handle(event, backend, sidebar_b)
                        }
                        _ => SidebarEvent::Ignored,
                    };
                    if result != SidebarEvent::Ignored {
                        redraw = true;
                    }
                }
                if ctx.in_sidebar(pos.x, pos.y) {
                    if let Some(sidebar_b) = ctx.sidebar_bounds() {
                        let panel = self.build_sidebar_action_panel(lh);
                        let layout = panel.layout(
                            sidebar_b,
                            quadraui::SidebarPanelMeasure::new(lh, 8.0),
                            toolbar_tui_measure,
                        );
                        if let Some(t) = layout.toolbar_layout.as_ref() {
                            redraw |= self.sidebar_action_bar_hover.update(t, pos.x, pos.y);
                        } else {
                            redraw |= self.sidebar_action_bar_hover.clear();
                        }
                        redraw |= self.panel_toolbar_hover.clear();
                    }
                } else if ctx.in_main(pos.x, pos.y) {
                    // #464: if a host-side selection drag is in progress,
                    // extend the selection to the current cell and redraw.
                    // This takes priority over the PTY motion path so the
                    // drag doesn't accidentally escape to the PTY.
                    let char_w = backend.char_width();
                    let main_b = ctx.main_bounds();
                    if self.terminal_host_sel_dragging && buttons.left {
                        if let Some((col, row)) =
                            self.active_terminal_pixel_to_cell(pos, main_b, lh, char_w)
                        {
                            self.terminal_host_sel_update(col, row);
                            redraw = true;
                        }
                    } else if buttons.left || buttons.right || buttons.middle {
                        // #454: forward cursor motion to the embedded PTY when
                        // mouse-reporting mode is active.  `forward_mouse`
                        // returns false when reporting is off, so there is no
                        // performance cost on the common idle path.
                        //
                        // Plain hover (no button held) is intentionally NOT
                        // forwarded: under the xterm "button-event tracking"
                        // protocol (mode 1002), a motion event always carries
                        // a button bit, so reporting Left for hover looks
                        // identical to a Left-drag — and would trigger vim
                        // visual selection, tmux copy-mode drag, ranger
                        // selection, etc. on plain cursor movement.  We
                        // forward Move only when at least one button is
                        // actually held (#454 review fix).
                        let btn = if buttons.right {
                            MouseButton::Right
                        } else if buttons.middle {
                            MouseButton::Middle
                        } else {
                            // buttons.left is true by the outer guard.
                            MouseButton::Left
                        };
                        redraw |= self.terminal_mouse_event(
                            TerminalMouseKind::Move,
                            btn,
                            pos,
                            Modifiers::default(),
                            main_b,
                            lh,
                            char_w,
                        );
                    }
                    // Note: intentional fall-through — toolbar hover tracking
                    // below still runs even when the PTY consumed the move.
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
                    // #438: hover tracking for the pipeline action bar
                    // ([ Go ⏎ ] / [ Retry ⏎ ] strip below the tab row).
                    // Pipeline has no panel toolbar so content starts at
                    // main_b.y + tab_h — compute bar_rect the same way
                    // the render path does and feed it to `toolbar_layout`.
                    if self.active_view == SidebarView::Pipeline
                        && self.pipeline_detail_tab == PipelineDetailTab::Pipeline
                    {
                        if let Some(action_toolbar) = self.pipeline_action_bar_toolbar() {
                            let main_b = ctx.main_bounds();
                            let tab_h = detail_tab_bar_height(lh);
                            let bar_h = pipeline_action_bar_height(true, lh);
                            let bar_rect =
                                Rect::new(main_b.x, main_b.y + tab_h, main_b.width, bar_h);
                            let layout = backend.toolbar_layout(bar_rect, &action_toolbar);
                            redraw |= self.pipeline_action_bar_hover.update(&layout, pos.x, pos.y);
                        } else {
                            redraw |= self.pipeline_action_bar_hover.clear();
                        }
                    } else {
                        redraw |= self.pipeline_action_bar_hover.clear();
                    }
                    redraw |= self.sidebar_action_bar_hover.clear();
                } else {
                    redraw |= self.sidebar_action_bar_hover.clear();
                    redraw |= self.panel_toolbar_hover.clear();
                    redraw |= self.pipeline_action_bar_hover.clear();
                }
                redraw
            }

            // Forward MouseUp to the active sidebar to release any scrollbar drag,
            // and forward the release to the embedded PTY when mouse reporting is on.
            UiEvent::MouseUp { button, position, .. } => {
                let pos = *position;
                let btn = *button;
                if let Some(sidebar_b) = ctx.sidebar_bounds() {
                    match self.active_view {
                        SidebarView::Board => {
                            self.board_sidebar.handle(event, backend, sidebar_b);
                        }
                        SidebarView::Pipeline => {
                            self.pipeline_sidebar.handle(event, backend, sidebar_b);
                        }
                        _ => {}
                    }
                }
                // #464: finalise a host-side selection drag on release.
                // Must run before the PTY forwarding path so we don't also
                // send a spurious Release to the PTY.
                if self.terminal_host_sel_dragging && btn == MouseButton::Left {
                    self.terminal_host_sel_end();
                    return true;
                }
                // #454: forward button release to the embedded PTY when mouse
                // reporting is enabled (gated inside forward_mouse itself).
                //
                // Two paths into the release:
                //   - Cursor still in_main → normal, position-driven
                //     forward via `terminal_mouse_event`.
                //   - Cursor outside in_main but we have an outstanding
                //     PTY press for this button → the user dragged out of
                //     the panel; force-forward with the position clamped
                //     to the PTY rect so the terminal app gets its
                //     matching Release (vim visual mode, tmux drag, less
                //     would otherwise stay "button held" forever).
                let bit = pty_button_bit(btn);
                let press_outstanding = bit != 0 && (self.pty_pressed_buttons & bit) != 0;
                let lh = backend.line_height();
                let char_w = backend.char_width();
                let main_b = ctx.main_bounds();
                let in_main = ctx.in_main(pos.x, pos.y);
                let mut consumed = false;
                if in_main {
                    consumed = self.terminal_mouse_event(
                        TerminalMouseKind::Release,
                        btn,
                        pos,
                        Modifiers::default(),
                        main_b,
                        lh,
                        char_w,
                    );
                }
                if press_outstanding && !consumed {
                    // Out-of-bounds release: clamp position to the PTY
                    // content rect and force-forward so the PTY sees the
                    // matching Release.  `terminal_force_release` handles
                    // both the standalone and Pipeline/Terminal sessions.
                    consumed = self.terminal_force_release(btn, pos, main_b, lh, char_w);
                }
                if press_outstanding {
                    self.pty_pressed_buttons &= !bit;
                }
                if consumed {
                    return true;
                }
                false
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
        let (sidebar_b, consumed) = self.hit_test_sidebar_action_bar(pos, sidebar_b, lh);
        let _ = backend; // backend reserved for hover updates wired below
        if consumed {
            return true;
        }
        // #646 focus-follows-click: any sidebar click blurs the terminal.
        if self.terminal_focused || self.detail_terminal_focused {
            self.terminal_focused = false;
            self.detail_terminal_focused = false;
        }
        match self.active_view {
            SidebarView::Board => {
                let result = self.board_sidebar.handle(event, backend, sidebar_b);
                match result {
                    SidebarEvent::RowSelected { section, ref path } => {
                        // #646 focus-follows-click: row click blurs the search filter.
                        if self.board_search.focused {
                            self.board_search.focused = false;
                            self.board_sidebar.focus_form(0, false);
                        }
                        if path.len() == 1 {
                            // #410: click on a milestone header — toggle milestone expansion.
                            let offset = self.board_repo_offset();
                            if section >= offset {
                                let repo_idx = section - offset;
                                if let Some(repo) = self.board_repo_names.get(repo_idx).cloned() {
                                    let milestone_idx = path[0] as usize;
                                    let cache = self.board_issues_cache.clone();
                                    let milestones = self.board_milestones_for_repo(&cache, &repo);
                                    if let Some((m_key, _, _)) = milestones.get(milestone_idx) {
                                        let m_key = m_key.clone();
                                        let entry = self
                                            .board_milestone_expanded
                                            .entry((repo, m_key))
                                            .or_insert(true);
                                        *entry = !*entry;
                                        self.rebuild_board_sidebar();
                                    }
                                }
                            }
                        } else {
                            // path.len() == 2: issue row — reset detail scroll.
                            self.detail_scroll = 0;
                        }
                        true
                    }
                    SidebarEvent::RowActivated { section, ref path } => {
                        // #646 focus-follows-click: row activate blurs the search filter.
                        if self.board_search.focused {
                            self.board_search.focused = false;
                            self.board_sidebar.focus_form(0, false);
                        }
                        if path.len() == 1 {
                            // Activate on a milestone header — toggle expansion.
                            let offset = self.board_repo_offset();
                            if section >= offset {
                                let repo_idx = section - offset;
                                if let Some(repo) = self.board_repo_names.get(repo_idx).cloned() {
                                    let milestone_idx = path[0] as usize;
                                    let cache = self.board_issues_cache.clone();
                                    let milestones = self.board_milestones_for_repo(&cache, &repo);
                                    if let Some((m_key, _, _)) = milestones.get(milestone_idx) {
                                        let m_key = m_key.clone();
                                        let entry = self
                                            .board_milestone_expanded
                                            .entry((repo, m_key))
                                            .or_insert(true);
                                        *entry = !*entry;
                                        self.rebuild_board_sidebar();
                                    }
                                }
                            }
                        } else {
                            // path.len() == 2: issue row activate — reset detail scroll.
                            self.detail_scroll = 0;
                        }
                        true
                    }
                    SidebarEvent::HeaderActivated { section: _ } => true,
                    SidebarEvent::FormEvent {
                        section: 0,
                        event: FormEvent::TextInputChanged { ref value, .. },
                    } => {
                        self.board_search.set_value(value);
                        self.rebuild_board_sidebar();
                        true
                    }
                    // Click on the filter TextInput focuses it (emits FocusChanged).
                    SidebarEvent::FormEvent {
                        section: 0,
                        event: FormEvent::FocusChanged { .. },
                    } => {
                        self.board_search.focused = true;
                        self.board_sidebar.focus_form(0, true);
                        true
                    }
                    // #410: Chevron click on a milestone header row (only depth-1 rows are headers).
                    SidebarEvent::RowToggleExpand { section, ref path } if path.len() == 1 => {
                        let offset = self.board_repo_offset();
                        if section >= offset {
                            let repo_idx = section - offset;
                            if let Some(repo) = self.board_repo_names.get(repo_idx).cloned() {
                                let cache = self.board_issues_cache.clone();
                                let milestones = self.board_milestones_for_repo(&cache, &repo);
                                let milestone_idx = path[0] as usize;
                                if let Some((m_key, _, _)) = milestones.get(milestone_idx) {
                                    let m_key = m_key.clone();
                                    let entry = self
                                        .board_milestone_expanded
                                        .entry((repo, m_key))
                                        .or_insert(true);
                                    *entry = !*entry;
                                    self.rebuild_board_sidebar();
                                }
                            }
                        }
                        true
                    }
                    SidebarEvent::StateChanged
                    | SidebarEvent::Consumed
                    | SidebarEvent::ScrollChanged { .. }
                    | SidebarEvent::FormEvent { .. }
                    | SidebarEvent::RowToggleExpand { .. } => true,
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
                let prev = self.pipeline_sel;
                let result = self.pipeline_sidebar.handle(event, backend, sidebar_b);
                self.pipeline_sel = self.selected_pipeline_index();
                if self.pipeline_sel != prev {
                    self.pipeline_focused_stage = self.default_focused_stage_for_selected_issue();
                    self.pipeline_stage_content_scroll = 0;
                }
                match result {
                    SidebarEvent::FormEvent {
                        section: 0,
                        event: FormEvent::TextInputChanged { ref value, .. },
                    } => {
                        self.pipeline_search.set_value(value);
                        self.rebuild_pipeline_sidebar(None);
                        true
                    }
                    // Click on the filter TextInput focuses it (emits FocusChanged).
                    SidebarEvent::FormEvent {
                        section: 0,
                        event: FormEvent::FocusChanged { .. },
                    } => {
                        self.pipeline_search.focused = true;
                        self.pipeline_sidebar.focus_form(0, true);
                        true
                    }
                    SidebarEvent::RowToggleExpand { section, ref path } if path.len() == 1 => {
                        // A one-level path = a repo/liveness sub-header was toggled.
                        // New/Done group by repo; Active groups by liveness (Live/Idle).
                        // Both persist expand state in pipeline_lifecycle_expanded
                        // keyed by (lc_key, group_key).
                        let search_offset = 1usize;
                        if section >= search_offset {
                            let state_idx = section - search_offset;
                            if let Some(&lc_key) = self.pipeline_state_section_names.get(state_idx)
                            {
                                let groups = if lc_key == "in-progress" {
                                    self.pipeline_active_by_liveness()
                                } else {
                                    self.pipeline_repos_for_state(lc_key)
                                };
                                let gi = path[0] as usize;
                                if let Some((group_key, _)) = groups.get(gi) {
                                    let group_key = group_key.clone();
                                    let entry = self
                                        .pipeline_lifecycle_expanded
                                        .entry((lc_key.to_string(), group_key))
                                        .or_insert(true);
                                    *entry = !*entry;
                                    self.rebuild_pipeline_sidebar(None);
                                }
                            }
                        }
                        true
                    }
                    SidebarEvent::RowToggleExpand { section, ref path } if path.len() == 2 => {
                        // #668: A two-level path = a milestone sub-header was
                        // toggled within a New section.  Persist the state in
                        // pipeline_milestone_expanded keyed by (lc_key, repo_key,
                        // milestone_key).  Refining/Pending have no milestone tier,
                        // so a path.len()==2 there is an issue row (not a header) —
                        // those sections handle selection via RowSelected, not here.
                        // #728: Done no longer has milestone sub-headers (flat list),
                        // so path.len()==2 in Done is an issue row — skip it here.
                        let search_offset = 1usize;
                        if section >= search_offset {
                            let state_idx = section - search_offset;
                            if let Some(&lc_key) = self.pipeline_state_section_names.get(state_idx)
                            {
                                if lc_key == "new" {
                                    let repo_groups = self.pipeline_repos_for_state(lc_key);
                                    let ri = path[0] as usize;
                                    let mi = path[1] as usize;
                                    if let Some((repo_key, repo_issue_idxs)) =
                                        repo_groups.get(ri)
                                    {
                                        let repo_key = repo_key.clone();
                                        let milestones = self
                                            .pipeline_milestones_for_issues(repo_issue_idxs);
                                        if let Some((mil_key, _, _)) = milestones.get(mi) {
                                            let mil_key = mil_key.clone();
                                            let entry = self
                                                .pipeline_milestone_expanded
                                                .entry((
                                                    lc_key.to_string(),
                                                    repo_key,
                                                    mil_key,
                                                ))
                                                .or_insert(true);
                                            *entry = !*entry;
                                            self.rebuild_pipeline_sidebar(None);
                                        }
                                    }
                                }
                            }
                        }
                        true
                    }
                    // #646 focus-follows-click: row click/activate blurs the search filter.
                    SidebarEvent::RowSelected { .. } | SidebarEvent::RowActivated { .. } => {
                        if self.pipeline_search.focused {
                            self.pipeline_search.focused = false;
                            self.pipeline_sidebar.focus_form(0, false);
                        }
                        true
                    }
                    SidebarEvent::HeaderActivated { .. }
                    | SidebarEvent::StateChanged
                    | SidebarEvent::Consumed
                    | SidebarEvent::ScrollChanged { .. }
                    | SidebarEvent::FormEvent { .. }
                    | SidebarEvent::RowToggleExpand { .. } => true,
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
            // #424: Terminal sidebar is a hint placeholder; clicks are inert.
            SidebarView::Terminal => false,
            // #638: Kanban sidebar is a placeholder; clicks are inert.
            SidebarView::Kanban => false,
            // #737: Merge Queue sidebar is a placeholder; clicks are inert.
            SidebarView::MergeQueue => false,
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
        let (content_main_b, toolbar_consumed) = self.hit_test_panel_toolbar(pos, main_b, lh);
        if toolbar_consumed {
            return true;
        }
        let main_b = content_main_b;
        // #646 focus-follows-click: click in main area blurs any active filter.
        if self.board_search.focused {
            self.board_search.focused = false;
            self.board_sidebar.focus_form(0, false);
        }
        if self.pipeline_search.focused {
            self.pipeline_search.focused = false;
            self.pipeline_sidebar.focus_form(0, false);
        }

        if self.active_view == SidebarView::Settings {
            // Route click to FormController. FormController::handle_cached
            // uses metrics cached by render_and_cache().
            let click_event = UiEvent::MouseDown {
                widget: None,
                button: MouseButton::Left,
                position: pos,
                modifiers: Modifiers::default(),
            };
            let result = self
                .settings_form
                .borrow_mut()
                .handle_cached(&click_event, main_b);
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
            let tab_h = detail_tab_bar_height(lh);
            if pos.y - main_b.y < tab_h {
                let bar = self.board_detail_tab_bar();
                let labels: Vec<&str> = bar.tabs.iter().map(|t| t.label.as_str()).collect();
                // Board has 4 tabs (#675 added Terminal) — unlikely to overflow at
                // typical widths, so scroll_offset is 0.
                if let Some(idx) = hit_tab_index_from_labels(&labels, main_b.x, pos.x, 0) {
                    let new_tab = match idx {
                        0 => BoardDetailTab::Board,
                        1 => BoardDetailTab::Issue,
                        2 => BoardDetailTab::Chat,
                        _ => BoardDetailTab::Terminal,
                    };
                    if new_tab != self.board_detail_tab {
                        // Mirror Pipeline tab handler: release PTY focus when
                        // switching away from the Terminal tab via mouse click.
                        if new_tab != BoardDetailTab::Terminal {
                            self.detail_terminal_focused = false;
                        }
                        self.board_detail_tab = new_tab;
                        self.detail_scroll = 0;
                        return true;
                    }
                }
                return false;
            }
            // #316: click in Chat tab content area — handle CTA button clicks.
            if self.board_detail_tab == BoardDetailTab::Chat && self.inject_chat.is_none() {
                let content_rect = Rect::new(
                    main_b.x,
                    main_b.y + tab_h,
                    main_b.width,
                    (main_b.height - tab_h).max(0.0),
                );
                let bar_h = lh * 2.0;
                let bar_rect = Rect::new(content_rect.x, content_rect.y, content_rect.width, bar_h);
                if pos.y >= bar_rect.y
                    && pos.y < bar_rect.y + bar_rect.height
                    && pos.x >= bar_rect.x
                    && pos.x < bar_rect.x + bar_rect.width
                {
                    // Two equal-width buttons: left half → Refine, right half → New Issue.
                    let mid = bar_rect.x + bar_rect.width / 2.0;
                    let action_id = if pos.x < mid {
                        "board-chat:refine"
                    } else {
                        "board-chat:new-issue"
                    };
                    return self.dispatch_toolbar_action(action_id);
                }
            }
            return false;
        }
        if self.active_view == SidebarView::Pipeline {
            // Tab bar occupies the first `detail_tab_bar_height(lh)` row of
            // the main panel — `(lh * 1.4).round()`, so the TUI and pixel
            // backends agree on the boundary (#464).
            let tab_h = detail_tab_bar_height(lh);
            if pos.y - main_b.y < tab_h {
                let bar = self.pipeline_detail_tab_bar();
                let labels: Vec<&str> = bar.tabs.iter().map(|t| t.label.as_str()).collect();
                // #605: match the painter's scroll-to-active offset so clicks
                // land on the right tab when the bar is scrolled on a narrow
                // width. The TUI tab_bar_layout computes this identically (same
                // width, same per-tab char measure, scroll arrows disabled).
                let active_idx = bar.tabs.iter().position(|t| t.is_active).unwrap_or(0);
                let tab_scroll = TabBar::fit_active_scroll_offset(
                    active_idx,
                    bar.tabs.len(),
                    main_b.width as usize,
                    |i| labels[i].chars().count(),
                );
                if let Some(idx) =
                    hit_tab_index_from_labels(&labels, main_b.x, pos.x, tab_scroll)
                {
                    let new_tab = match idx {
                        0 => PipelineDetailTab::Pipeline,
                        1 => PipelineDetailTab::Issue,
                        2 => PipelineDetailTab::Stages,
                        3 => PipelineDetailTab::Log,
                        4 => PipelineDetailTab::Summary,
                        5 => PipelineDetailTab::Refinement,
                        _ => PipelineDetailTab::Terminal,
                    };
                    if new_tab != self.pipeline_detail_tab {
                        if new_tab != PipelineDetailTab::Terminal {
                            self.detail_terminal_focused = false;
                        }
                        self.pipeline_detail_tab = new_tab;
                        self.pipeline_detail_scroll = if new_tab == PipelineDetailTab::Log {
                            usize::MAX
                        } else {
                            0
                        };
                        if new_tab == PipelineDetailTab::Log {
                            self.ensure_log_tab_sse();
                        }
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
                    // #303: click on the button bar dispatches the active
                    // action. Bar lives at the top of content_rect when any
                    // stage has a dispatchable action.
                    let action_btn = self.pipeline_action_button();
                    let bar_h = pipeline_action_bar_height(action_btn.is_some(), lh);
                    if bar_h > 0.0
                        && pos.y >= content_rect.y
                        && pos.y < content_rect.y + bar_h
                        && pos.x >= content_rect.x
                        && pos.x < content_rect.x + content_rect.width
                    {
                        if let Some((_, stage_idx)) = action_btn {
                            self.dispatch_pipeline_stage(stage_idx);
                            return true;
                        }
                    }
                    // Stage row sits below the bar.
                    let pv_origin = Rect::new(
                        content_rect.x,
                        content_rect.y + bar_h,
                        content_rect.width,
                        (content_rect.height - bar_h).max(0.0),
                    );
                    let pv_rect = pipeline_detail_pv_rect(pv_origin, lh);
                    // Match the render path: stripped view → action_height=0,
                    // so action_bounds is always None and only Body hits fire.
                    let render_view = pipeline_view_for_render(&view);
                    let layout = tui_pipeline_layout(&render_view, pv_rect);
                    match layout.hit_test(pos.x, pos.y) {
                        PipelineHit::Action(stage_idx) => {
                            // Defensive: with action-stripped view this branch
                            // shouldn't fire, but keep the dispatch path wired
                            // in case a future stage carries an action again.
                            self.dispatch_pipeline_stage(stage_idx);
                            return true;
                        }
                        PipelineHit::Body(stage_idx) => {
                            // Click on a stage box — set focus so the content
                            // panel below switches to this stage's output.
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
        // #638: Kanban view — hit-test click against last known board layout.
        if self.active_view == SidebarView::Kanban {
            let hit = self.kanban_layout.borrow().as_ref().map(|l| l.hit_test(pos.x, pos.y));
            match hit {
                Some(BoardHit::Card(ref card_id)) => {
                    // Second click on already-selected card → open in Board view.
                    if self.kanban_model.selected_card_id.as_ref() == Some(card_id) {
                        let id = card_id.clone();
                        self.kanban_open_card(&id);
                    } else {
                        self.kanban_model.selected_card_id = Some(card_id.clone());
                    }
                    return true;
                }
                Some(BoardHit::ColumnHeader(_)) | Some(BoardHit::Empty) | None => {}
            }
            return false;
        }
        false
    }

    /// Forward a mouse event to the active embedded terminal PTY (standalone
    /// `SidebarView::Terminal` or `PipelineDetailTab::Terminal`).
    ///
    /// Returns `true` when the PTY consumed the event — caller should trigger
    /// a redraw and skip any local fallback handling.  Returns `false` when
    /// - no terminal view is active,
    /// - the cursor is outside the terminal content area, or
    /// - `forward_mouse` reports that the PTY has mouse reporting off (for
    ///   Press/Release/Move) or neither mouse reporting nor alt-screen
    ///   active (for wheel events).
    ///
    /// The coordinate translation mirrors the render path: for the standalone
    /// terminal, `main_b` is the full main content rect (no toolbar).  For the
    /// detail terminal, `main_b.y + lh*1.4` is the content-area top edge.
    fn terminal_mouse_event(
        &mut self,
        kind: TerminalMouseKind,
        button: MouseButton,
        pos: Point,
        modifiers: Modifiers,
        main_b: Rect,
        lh: f32,
        char_w: f32,
    ) -> bool {
        match self.active_view {
            SidebarView::Terminal => {
                // Terminal surface occupies the full main content rect (the
                // Terminal view has no panel toolbar, so main_b == the PTY area).
                if let Some((col, row)) =
                    terminal_pixel_to_cell(pos, main_b, main_b.y, char_w, lh)
                {
                    if let Some(ref mut sess) = self.terminal_session {
                        return sess.forward_mouse(kind, button, col, row, modifiers);
                    }
                }
                false
            }
            SidebarView::Pipeline
                if self.pipeline_detail_tab == PipelineDetailTab::Terminal =>
            {
                // Content area starts below the tab bar. `#464`: use the
                // rounded helper so the hit-test origin lines up with the
                // render origin in the TUI backend (where `q_rect_to_ratatui`
                // rounds the fractional `lh * 1.4` to a whole cell).
                let content_y = main_b.y + detail_tab_bar_height(lh);
                if let Some((col, row)) =
                    terminal_pixel_to_cell(pos, main_b, content_y, char_w, lh)
                {
                    if let Some(issue_key) = self.selected_issue_key() {
                        if let Some(sess) = self.detail_terminal_sessions.get_mut(&issue_key) {
                            return sess.forward_mouse(kind, button, col, row, modifiers);
                        }
                    }
                }
                false
            }
            _ => false,
        }
    }

    /// #454: force-forward a `Release` to the active terminal session even
    /// when `pos` lies outside the PTY content rect.  Mirrors
    /// [`terminal_mouse_event`]'s routing (standalone Terminal vs
    /// Pipeline/Terminal tab) but uses [`terminal_pixel_to_cell_clamped`]
    /// so the cell coordinates land inside the visible grid.
    ///
    /// Called from the `MouseUp` arm when `pty_pressed_buttons` records an
    /// outstanding press for `button` — without this fallback, terminal
    /// apps that opted into mouse reporting would stay stuck in
    /// "button held" state after the user drags out of the panel
    /// (vim visual mode, tmux mouse drag, less, ranger, …).
    fn terminal_force_release(
        &mut self,
        button: MouseButton,
        pos: Point,
        main_b: Rect,
        lh: f32,
        char_w: f32,
    ) -> bool {
        match self.active_view {
            SidebarView::Terminal => {
                let (col, row) =
                    terminal_pixel_to_cell_clamped(pos, main_b, main_b.y, char_w, lh);
                if let Some(ref mut sess) = self.terminal_session {
                    return sess.forward_mouse(
                        TerminalMouseKind::Release,
                        button,
                        col,
                        row,
                        Modifiers::default(),
                    );
                }
                false
            }
            SidebarView::Pipeline
                if self.pipeline_detail_tab == PipelineDetailTab::Terminal =>
            {
                // `#464`: rounded helper for parity with the render path.
                let content_y = main_b.y + detail_tab_bar_height(lh);
                let (col, row) =
                    terminal_pixel_to_cell_clamped(pos, main_b, content_y, char_w, lh);
                if let Some(issue_key) = self.selected_issue_key() {
                    if let Some(sess) = self.detail_terminal_sessions.get_mut(&issue_key) {
                        return sess.forward_mouse(
                            TerminalMouseKind::Release,
                            button,
                            col,
                            row,
                            Modifiers::default(),
                        );
                    }
                }
                false
            }
            _ => false,
        }
    }

    // ── #464: host-side terminal selection helpers ────────────────────────────

    /// Translate a pixel position to a terminal cell `(col, row)` for
    /// whichever terminal view is currently active (standalone Terminal or
    /// Pipeline / Terminal tab).  Returns `None` when `pos` is outside the
    /// PTY content area.
    fn active_terminal_pixel_to_cell(
        &self,
        pos: Point,
        main_b: Rect,
        lh: f32,
        char_w: f32,
    ) -> Option<(u16, u16)> {
        match self.active_view {
            SidebarView::Terminal => {
                terminal_pixel_to_cell(pos, main_b, main_b.y, char_w, lh)
            }
            SidebarView::Pipeline
                if self.pipeline_detail_tab == PipelineDetailTab::Terminal =>
            {
                // `#464`: rounded helper for parity with the render path.
                let content_y = main_b.y + detail_tab_bar_height(lh);
                terminal_pixel_to_cell(pos, main_b, content_y, char_w, lh)
            }
            _ => None,
        }
    }

    /// Return a mutable reference to the currently active embedded terminal
    /// session, or `None` when no terminal view is active or no session exists.
    fn active_terminal_session_mut(
        &mut self,
    ) -> Option<&mut quadraui::terminal_engine::TerminalSession> {
        match self.active_view {
            SidebarView::Terminal => self.terminal_session.as_mut(),
            SidebarView::Pipeline
                if self.pipeline_detail_tab == PipelineDetailTab::Terminal =>
            {
                let key = self.selected_issue_key()?;
                self.detail_terminal_sessions.get_mut(&key)
            }
            _ => None,
        }
    }

    /// Return the selected text from the active terminal session, if any.
    fn active_terminal_selected_text(&self) -> Option<String> {
        match self.active_view {
            SidebarView::Terminal => self.terminal_session.as_ref()?.selected_text(),
            SidebarView::Pipeline
                if self.pipeline_detail_tab == PipelineDetailTab::Terminal =>
            {
                let key = self.selected_issue_key()?;
                self.detail_terminal_sessions.get(&key)?.selected_text()
            }
            _ => None,
        }
    }

    /// Clear the selection in the active terminal session.
    fn clear_active_terminal_selection(&mut self) {
        if let Some(sess) = self.active_terminal_session_mut() {
            sess.selection = None;
        }
    }

    /// Begin a host-side selection drag in the active terminal at `(col, row)`.
    /// Clears any previous selection and sets the anchor to `(col, row)`.
    /// No-op (dragging flag not set) when there is no active terminal session.
    fn terminal_host_sel_begin(&mut self, col: u16, row: u16) {
        use quadraui::terminal_engine::TerminalSelection;
        if let Some(sess) = self.active_terminal_session_mut() {
            sess.selection = Some(TerminalSelection {
                start_row: row,
                start_col: col,
                end_row: row,
                end_col: col,
            });
        } else {
            return;
        }
        self.terminal_host_sel_dragging = true;
    }

    /// Extend the host-side selection drag to `(col, row)` (move event).
    /// No-op when no drag is in progress.
    fn terminal_host_sel_update(&mut self, col: u16, row: u16) {
        if !self.terminal_host_sel_dragging {
            return;
        }
        if let Some(sess) = self.active_terminal_session_mut() {
            if let Some(ref mut sel) = sess.selection {
                sel.end_row = row;
                sel.end_col = col;
            }
        }
    }

    /// Finalise the host-side selection drag (release event).
    /// The selection remains visible; only the dragging flag is cleared.
    /// A collapsed selection (anchor == end) is cleared entirely since it
    /// represents a plain click with no text chosen.
    fn terminal_host_sel_end(&mut self) {
        self.terminal_host_sel_dragging = false;
        // Clear collapsed (point) selections — they're just phantom clicks.
        if let Some(sess) = self.active_terminal_session_mut() {
            if matches!(&sess.selection, Some(sel)
                if sel.start_row == sel.end_row && sel.start_col == sel.end_col)
            {
                sess.selection = None;
            }
        }
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
            // #424: Terminal view's sidebar is just a hint placeholder —
            // no scrollable content, swallow the wheel.
            SidebarView::Terminal => false,
            // #638: Kanban sidebar is a placeholder — no sidebar scroll.
            SidebarView::Kanban => false,
            // #737: Merge Queue sidebar is a placeholder — no sidebar scroll.
            SidebarView::MergeQueue => false,
        }
    }

    /// Scroll wheel in the main panel (detail / machine detail).
    fn mouse_main_scroll(&mut self, delta: ScrollDelta, pos: Point, main_b: Rect, lh: f32, char_w: f32) -> bool {
        let visible = content_visible_rows(main_b, lh);
        // Stash the live viewport size — `watch_log_list` uses this to compute
        // a stick-to-bottom offset that keeps the last line on screen.
        self.last_main_visible_rows.set(visible.max(1));
        // Watch overlay takes over the main panel; route scrollwheel to it
        // regardless of which view is active underneath.  The Log tab is
        // unaffected: it only seeds `watch_pool` (no `watch_focused`), so this
        // gate is false there and the wheel falls through to the Log scroller.
        if self.watch_focused.is_some() {
            // SSE log lines drive the count when present; fall back to the
            // remote-log cache when SSE isn't yet connected.
            let items = self
                .watch_focused
                .as_ref()
                .and_then(|id| self.watch_pool.get(id))
                .map(|ctx| ctx.sse.lines.len())
                .unwrap_or_else(|| self.watch_log_list().items.len());
            let max = items.saturating_sub(visible.saturating_sub(1));
            if let Some(id) = self.watch_focused.clone() {
                if let Some(ctx) = self.watch_pool.get_mut(&id) {
                    let w = &mut ctx.state;
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
                        w.scroll = if new_scroll >= max {
                            usize::MAX
                        } else {
                            new_scroll
                        };
                    }
                }
            }
            return true;
        }
        match self.active_view {
            SidebarView::Board => {
                // Use the active tab's actual list so the scroll max matches
                // what's rendered. Board tab → detail_list; Issue tab → body.
                // Chat tab scroll is handled by ChatController itself.
                let items = match self.board_detail_tab {
                    BoardDetailTab::Board => self.detail_list().items.len(),
                    BoardDetailTab::Issue => self.board_issue_body_list().items.len(),
                    BoardDetailTab::Chat => 0,
                    BoardDetailTab::Terminal => 0, // #675: scroll handled by the PTY session
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
                // Issue tab body and Stages tab content (rendered plan, log
                // tails) can both overflow the panel.  The Pipeline tab is a
                // fixed-size widget — scroll there is consumed but inert.
                match self.pipeline_detail_tab {
                    PipelineDetailTab::Issue => {
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
                    PipelineDetailTab::Stages => {
                        let items = self.pipeline_stages_list().items.len();
                        let max = items.saturating_sub(visible.saturating_sub(1));
                        if delta.y > 0.0 {
                            self.pipeline_stage_content_scroll =
                                self.pipeline_stage_content_scroll.saturating_sub(1);
                        } else if delta.y < 0.0 {
                            self.pipeline_stage_content_scroll =
                                (self.pipeline_stage_content_scroll + 1).min(max);
                        }
                    }
                    PipelineDetailTab::Pipeline => {
                        // The body list (meta + stage content) is now
                        // scrollable on the Pipeline tab.
                        let items = self.pipeline_tab_body_list().items.len();
                        let max = items.saturating_sub(visible.saturating_sub(1));
                        if delta.y > 0.0 {
                            self.pipeline_stage_content_scroll =
                                self.pipeline_stage_content_scroll.saturating_sub(1);
                        } else if delta.y < 0.0 {
                            self.pipeline_stage_content_scroll =
                                (self.pipeline_stage_content_scroll + 1).min(max);
                        }
                    }
                    PipelineDetailTab::Log => {
                        let items = self.pipeline_log_list().items.len();
                        let visible_rows = visible.max(1);
                        let max = items.saturating_sub(visible_rows.saturating_sub(1));
                        let current = if self.pipeline_detail_scroll == usize::MAX {
                            max
                        } else {
                            self.pipeline_detail_scroll
                        };
                        if delta.y > 0.0 {
                            // Scroll up breaks sticky.
                            self.pipeline_detail_scroll = current.saturating_sub(1);
                        } else if delta.y < 0.0 {
                            let new = (current + 1).min(max);
                            // Re-stick when reaching the bottom.
                            self.pipeline_detail_scroll = if new >= max { usize::MAX } else { new };
                        }
                    }
                    PipelineDetailTab::Summary => {
                        // #558: plain scroll — same pattern as the Issue tab.
                        let items = self.pipeline_summary_list().items.len();
                        let max = items.saturating_sub(visible.saturating_sub(1));
                        if delta.y > 0.0 {
                            self.pipeline_detail_scroll =
                                self.pipeline_detail_scroll.saturating_sub(1);
                        } else if delta.y < 0.0 {
                            self.pipeline_detail_scroll =
                                (self.pipeline_detail_scroll + 1).min(max);
                        }
                    }
                    PipelineDetailTab::Refinement => {
                        // #264: ChatController owns its own scroll inside
                        // the embedded transcript when the chat is bound
                        // to this issue; the parent panel-scroll path is
                        // a no-op so wheel events here don't fight with
                        // the chat's internal scrollbar.
                    }
                    PipelineDetailTab::Terminal => {
                        // #454: Forward scroll to the PTY when the child has
                        // mouse reporting or is on the alt screen; otherwise
                        // scroll local scrollback (3 rows per notch).
                        //
                        // `forward_mouse` for `WheelUp`/`WheelDown` already
                        // gates internally on
                        // `should_forward_wheel()` (mouse reporting OR
                        // alt-screen), so calling it directly and falling
                        // back on `false` is equivalent to the explicit
                        // gate from the issue spec.
                        //
                        // `#464`: rounded helper for parity with the render
                        // path so the click-to-cell mapping is exact in TUI.
                        let content_y = main_b.y + detail_tab_bar_height(lh);
                        if delta.y != 0.0 {
                            if let Some((col, row)) =
                                terminal_pixel_to_cell(pos, main_b, content_y, char_w, lh)
                            {
                                let kind = if delta.y > 0.0 {
                                    TerminalMouseKind::WheelUp
                                } else {
                                    TerminalMouseKind::WheelDown
                                };
                                if let Some(issue_key) = self.selected_issue_key() {
                                    if let Some(sess) =
                                        self.detail_terminal_sessions.get_mut(&issue_key)
                                    {
                                        if !sess.forward_mouse(
                                            kind,
                                            MouseButton::Left,
                                            col,
                                            row,
                                            Modifiers::default(),
                                        ) {
                                            // No PTY mouse reporting — scroll local scrollback.
                                            if delta.y > 0.0 {
                                                sess.scroll_up(3);
                                            } else {
                                                sess.scroll_down(3);
                                            }
                                        }
                                    }
                                }
                            }
                        }
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
                self.settings_form
                    .borrow_mut()
                    .handle_cached(&scroll_event, main_b);
                true
            }
            // #454: Terminal pane — forward scroll wheel to the PTY when
            // the child has mouse reporting enabled or is on the alt screen
            // (e.g. tmux, vim, less).  Fall back to local scrollback when
            // forward_mouse returns false.
            //
            // `forward_mouse` for wheel kinds gates internally on
            // `should_forward_wheel()` (mouse reporting OR alt-screen),
            // matching the explicit gate from the issue spec.
            SidebarView::Terminal => {
                if delta.y != 0.0 {
                    if let Some((col, row)) =
                        terminal_pixel_to_cell(pos, main_b, main_b.y, char_w, lh)
                    {
                        if let Some(ref mut sess) = self.terminal_session {
                            let kind = if delta.y > 0.0 {
                                TerminalMouseKind::WheelUp
                            } else {
                                TerminalMouseKind::WheelDown
                            };
                            if !sess.forward_mouse(
                                kind,
                                MouseButton::Left,
                                col,
                                row,
                                Modifiers::default(),
                            ) {
                                // No PTY mouse reporting — scroll local scrollback.
                                if delta.y > 0.0 {
                                    sess.scroll_up(3);
                                } else {
                                    sess.scroll_down(3);
                                }
                            }
                        }
                    }
                }
                true
            }
            // #638: Kanban — mouse wheel scroll not yet implemented in v1;
            // keyboard navigation (j/k/h/l) works.
            SidebarView::Kanban => true,
            // #737: Merge Queue panel — j/k handles navigation; wheel is no-op for now.
            SidebarView::MergeQueue => true,
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
                text: format!(
                    " {}  [1=Board 2=Machines 3=Pipeline 4=Settings 5=Terminal 6=Kanban 7=MQ] ",
                    view_label
                ),
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
        // #628: persistent indicator for live interactive sessions. Detached
        // sessions create no board `running` row, so they were invisible —
        // they only showed in the one-time #487 startup toast and then piled
        // up unseen (burning Claude Max quota, holding worktrees). This durable
        // badge — refreshed by the background `coord sessions --remote`
        // discovery, and independent of any board row's status (a merged row
        // moves to Done but its session may still be live) — means you can
        // never silently accumulate them again.
        // #491: dead-pane sessions are shown with an amber warning badge;
        // alive sessions keep the normal blue badge.
        if !self.live_tmux_sessions.is_empty() {
            let n = self.live_tmux_sessions.len();
            let n_dead = self.live_tmux_sessions.iter().filter(|s| s.pane_dead).count();
            let badge_text = if n_dead > 0 {
                format!(
                    " ◉ {} session{} ({} dead) ",
                    n,
                    if n == 1 { "" } else { "s" },
                    n_dead
                )
            } else {
                format!(" ◉ {} live session{} ", n, if n == 1 { "" } else { "s" })
            };
            let (badge_fg, badge_bg) = if n_dead > 0 {
                // Amber: dead-pane sessions need attention.
                (Color::rgb(255, 210, 100), Color::rgb(70, 45, 10))
            } else {
                // Normal blue for all-alive sessions.
                (Color::rgb(150, 210, 255), Color::rgb(20, 45, 70))
            };
            left.push(StatusBarSegment {
                text: badge_text,
                fg: badge_fg,
                bg: badge_bg,
                bold: true,
                action_id: None,
            });
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
            let depth = self.command_runner.queue_depth();
            let text = if depth > 0 {
                format!(
                    " {} ({:.0}s) · {} queued ",
                    label,
                    elapsed.as_secs_f64(),
                    depth
                )
            } else {
                format!(" {} ({:.0}s) ", label, elapsed.as_secs_f64())
            };
            left.push(StatusBarSegment {
                text,
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
        // #369/#329: Prompt questions are now shown as quadraui Dialog
        // popups rather than status-bar hint strings — the dialog is the
        // affordance.  The hints chain below falls through to the normal
        // view-specific hints whenever a prompt dialog is showing, so the
        // status bar remains informative instead of going blank.
        let hints = if self.active_view == SidebarView::Terminal {
            // #424: Terminal pane — F12 toggles PTY focus / chrome focus.
            // #605: Ctrl-W is the keyboard pane leader (Ctrl-W h = side panel).
            if self.terminal_focused {
                " PTY focused — F12 / Ctrl-W h = release  (typed keys go to the shell) ".to_string()
            } else {
                " PTY released — F12 / Ctrl-W l = focus  ·  1–5 switch view  ·  q=quit ".to_string()
            }
        } else if self.active_view == SidebarView::Pipeline
            && self.pipeline_detail_tab == PipelineDetailTab::Terminal
        {
            // #440/#467: Pipeline detail Terminal tab — same F12 model as
            // the standalone pane but scoped to the selected issue's session.
            // `s` (released only) launches a local `coord assign --interactive`
            // claude session for the selected issue.
            if self.detail_terminal_focused {
                " PTY focused — F12 / Ctrl-W h = release  (typed keys go to the shell) ".to_string()
            } else {
                " PTY released — F12 / Ctrl-W l = focus  ·  s = interactive claude  ·  j/k=nav  h/l=tabs  Ctrl-W h=side panel  q=quit ".to_string()
            }
        } else if self.active_view == SidebarView::Board
            && self.board_detail_tab == BoardDetailTab::Terminal
        {
            // #675: Board detail Terminal tab — same F12 model as Pipeline.
            // The `s` shortcut is not available here (Board Terminal is for Chat).
            if self.detail_terminal_focused {
                " PTY focused — F12 to release  (typed keys go to the shell) ".to_string()
            } else {
                " PTY released — F12 to focus  ·  h/l=tabs  q=quit ".to_string()
            }
        } else if self.active_view == SidebarView::Machines {
            // Machines panel action hints.
            " r=restart  u=update  c=clean worktrees  p=pause/unpause  q=quit ".to_string()
        } else if self.active_view == SidebarView::Pipeline && self.test_gate_actionable() {
            // #200: surface the Test gate keybinds when actionable for the
            // currently-selected pipeline issue.
            // #235: include B (Phase 1 build) when no build is already in
            // flight for this work id.  #314: always include T=test-chat.
            if self.can_trigger_test_build() {
                " Test gate: B=build  T=chat  1–8=run step  P=pass  F=fail  r=report+fix  S=skip  q=quit ".to_string()
            } else {
                " Test gate: T=chat  1–8=run step  P=pass  F=fail  r=report+fix  S=skip  q=quit "
                    .to_string()
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
                format!(
                    "{} +{}",
                    summary.failed_names[..2].join(", "),
                    summary.failed_names.len() - 2
                )
            } else {
                summary.failed_names.join(", ")
            };
            format!(" Checks failed: {}  m=merge anyway  q=quit ", names)
        } else if self.merge_blocked_on_review_for_selected_issue() {
            // #253: the selected merge entry has no approved review on the
            // board.  Surface the block so the user doesn't press lowercase
            // `m` and silently merge unreviewed code.  Capital `M` runs
            // `coord merge --skip-review` for the deliberate override.
            " Merge blocked: review not yet approved  R=dispatch review  M=merge anyway  q=quit "
                .to_string()
        } else if self.active_view == SidebarView::Pipeline {
            // #194: Pipeline-specific hints: refresh, navigate, go, dismiss done.
            // #386: include i=steer on the Log tab so the feature is discoverable.
            // #628: include L=sessions when live sessions are present.
            let live_hint = if !self.live_tmux_sessions.is_empty() {
                "  L=sessions"
            } else {
                ""
            };
            if self.pipeline_detail_tab == PipelineDetailTab::Log {
                format!(
                    " j/k=nav  Enter=go  i=steer  R=refresh  D=dismiss-done  h/l=tabs  < >=hscroll{}  q=quit ",
                    live_hint
                )
            } else {
                format!(
                    " j/k=nav  Enter=go  R=refresh  D=dismiss-done  h/l=tabs{}  q=quit ",
                    live_hint
                )
            }
        } else if self.active_view == SidebarView::MergeQueue {
            // #737 / #780: Merge Queue panel hints.
            let attn = if self.merge_queue_needs_attention() {
                " ⚠ needs attention!"
            } else {
                ""
            };
            format!(
                " j/k=nav  a=merge-all-ready  m=merge-only-this  M=force-merge  d=drop  s=interactive{}  q=quit ",
                attn
            )
        } else {
            // #192: `p` / `a` / `A` retired alongside the PROPOSALS
            // section.  Right-click → Send to Pipeline (#261) is the
            // canonical dispatch path now.
            // #628: include L=sessions when live sessions are present.
            let _ = proposals;
            let live_hint = if !self.live_tmux_sessions.is_empty() {
                "  L=sessions"
            } else {
                ""
            };
            format!(" n=notify  m=merge  R=retry  P=purge{}  q=quit ", live_hint)
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


/// #319 Phase A: today's date as `YYYY-MM-DD`, computed from `SystemTime`
/// via Howard Hinnant's `days_from_civil` algorithm (the same one chrono,
/// time-rs, and the C++ standard library use).  Pulled in by hand to
/// avoid a chrono dependency for a single use site.
fn today_yyyy_mm_dd() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    // Days since 1970-01-01 (UTC).  Local time is "close enough" for a
    // header date stamp; off-by-a-day at most.
    let days = secs.div_euclid(86_400);
    // Shift so the epoch is 0000-03-01 (start of a Hinnant "era").
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = y + if m <= 2 { 1 } else { 0 };
    format!("{:04}-{:02}-{:02}", y, m, d)
}


/// #pause: read the set of paused machines from disk.  Shared with the
/// Python coordinator (`coord pause` / `coord unpause` write the same
/// file) so the TUI's filter agrees with `coord plan`, auto_loop,
/// reconcile, review, and refine-chat dispatches.  Returns empty on any
/// I/O or parse failure — failure to read should never block routing.
fn read_paused_machines() -> std::collections::HashSet<String> {
    let mut out = std::collections::HashSet::new();
    let home = match std::env::var_os("HOME") {
        Some(h) => std::path::PathBuf::from(h),
        None => return out,
    };
    let path = home.join(".coord").join("paused_machines.json");
    let content = match std::fs::read_to_string(&path) {
        Ok(c) => c,
        Err(_) => return out,
    };
    // Tiny hand-rolled extract — the file is `{"paused": ["foo", "bar"]}`
    // and pulling in serde just for this would inflate dependencies.
    // Walk the `paused` array, collect bare-string entries.
    let lower = content.to_string();
    let key_pos = match lower.find("\"paused\"") {
        Some(p) => p,
        None => return out,
    };
    let after_key = &content[key_pos..];
    let bracket_start = match after_key.find('[') {
        Some(p) => p,
        None => return out,
    };
    let bracket_end = match after_key[bracket_start..].find(']') {
        Some(p) => bracket_start + p,
        None => return out,
    };
    let inner = &after_key[bracket_start + 1..bracket_end];
    let mut chars = inner.chars().peekable();
    let mut current = String::new();
    let mut in_string = false;
    while let Some(c) = chars.next() {
        if in_string {
            if c == '"' {
                if !current.is_empty() {
                    out.insert(std::mem::take(&mut current));
                }
                in_string = false;
            } else if c == '\\' {
                if let Some(esc) = chars.next() {
                    current.push(esc);
                }
            } else {
                current.push(c);
            }
        } else if c == '"' {
            in_string = true;
            current.clear();
        }
    }
    out
}

/// Pick a single short line out of a child command's stderr suitable for a
/// toast body.  Skips blank lines and Python/Click tracebacks/prefixes (which
/// the coord CLI emits before the actual error message) so the user gets the
/// real reason — e.g. `gh failed: 'status:refining' not found` rather than
/// the wrapping `Error:`/`Traceback…` boilerplate.
fn first_meaningful_stderr_line(stderr: &str) -> Option<String> {
    let trimmed = stderr.trim();
    if trimmed.is_empty() {
        return None;
    }
    // Prefer the LAST non-empty line — coord error paths end with the
    // operative message (`gh failed: …`, `failed to update N issues`), and
    // Click tracebacks tend to pile prelude lines before it.
    let last = trimmed
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .last()?;
    // Cap length for a toast.
    let max = 200usize;
    if last.len() > max {
        let mut out = last[..max].to_string();
        out.push('…');
        Some(out)
    } else {
        Some(last.to_string())
    }
}

/// #303: Strip every stage's `action` field so quadraui's `draw_pipeline_view`
/// renders the boxes as purely informational (status icon + label + elapsed),
/// no inline `[Go]`/`[Retry]` button.  The action info lives on the widget
/// returned by `build_pipeline_widget` (used by `dispatch_pipeline_active_go`
/// and `pipeline_action_button`); the visible buttons move to the bar above.
///
/// Pass the *stripped* view to both `draw_pipeline_view` AND
/// `tui_pipeline_layout` so paint and hit-test agree on the (now-shrunk)
/// stage-box bounds — quadraui drops the action row when no stage has one.




#[cfg(test)]
mod tests;
