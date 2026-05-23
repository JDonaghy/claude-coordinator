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
use quadraui::compose::sidebar_system::{
    NavigationMode, SidebarEvent, SidebarSectionDef, SidebarSystem,
};
use quadraui::primitives::form::{FieldKind, Form, FormEvent, FormField};
use quadraui::{
    Backend, Badge, Color, Decoration, Key, ListItem, ListView, MouseButton, NamedKey,
    PipelineHit, PipelineStage as QuiPipelineStage, PipelineView as QuiPipelineView,
    Point, Reaction, Rect, ScrollDelta, ScrollMode, SectionSize, ShellApp,
    ShellConfig, ShellContext, StageStatus, StatusBar, StatusBarSegment, StyledSpan, StyledText,
    TabBar, TabItem, TreeRow, UiEvent, WidgetId,
};

// ─── Auto-refresh interval ────────────────────────────────────────────────────

/// Reload board data every 5 seconds.
const REFRESH_EVERY: Duration = Duration::from_secs(5);

/// Auto-run `coord notify` every 30 seconds when assignments are running.
const NOTIFY_EVERY: Duration = Duration::from_secs(30);

// ─── Detail panel tabs ────────────────────────────────────────────────────────

/// The two tabs shown in the Board view detail panel.
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
enum DetailTab {
    /// Static assignment info (ID, machine, status, branch, etc.).
    #[default]
    Summary,
    /// Live feed of worker events parsed from the log file.
    Activity,
}

/// The two tabs shown in the Pipeline view detail panel.
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
enum PipelineDetailTab {
    /// Horizontal stage view + repo/labels/gates meta.
    #[default]
    Pipeline,
    /// Full issue body text (scrollable with j/k).
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
}

impl SidebarView {
    fn label(self) -> &'static str {
        match self {
            SidebarView::Board => "Board",
            SidebarView::Machines => "Machines",
            SidebarView::Pipeline => "Pipeline",
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
}

impl IssueGroup {
    fn status_icon(&self) -> &str {
        match self.status_summary.as_str() {
            "running" => "~",
            "failed" => "✗",
            "done" | "merged" => "✓",
            _ => "-",
        }
    }

    fn status_color(&self) -> Color {
        match self.status_summary.as_str() {
            "running" => Color::rgb(80, 220, 80),
            "failed" => Color::rgb(220, 70, 70),
            "done" => Color::rgb(120, 180, 120),
            "merged" => Color::rgb(100, 180, 240),
            _ => Color::rgb(140, 140, 160),
        }
    }
}

#[derive(Clone)]
#[allow(dead_code)] // assignment_id and pr_url stored for future display
struct MergeQueueEntry {
    assignment_id: String,
    issue_number: Option<u64>,
    state: String,
    pr_number: Option<i64>,
    pr_url: Option<String>,
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
/// Sourced from a background `gh search issues label:<L> state:open` poll
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
    /// Tracked labels that flagged this issue.
    matched_labels: Vec<String>,
}

#[derive(Default)]
/// An open issue from the local `issues` table (synced from GitHub on coord plan).
#[derive(Clone)]
struct OpenIssue {
    repo_name: String,
    number: u64,
    title: String,
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
        // "name" should appear within the next ~200 chars of the same object.
        let window_end = (after + 200).min(json.len());
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
        decoration: if entry.map(|e| e.state.as_str()) == Some("failed") {
            Decoration::Error
        } else {
            Decoration::Normal
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
             status, branch, model, type, dispatched_at, finished_at, exit_code \
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
            "SELECT mq.assignment_id, a.issue_number, mq.state, mq.pr_number, mq.pr_url \
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

    // ── Query open issues (synced from GitHub on coord plan) ──────────────
    let open_issues: Vec<OpenIssue> = {
        let mut stmt = match conn.prepare(
            "SELECT repo_name, number, title FROM issues WHERE state = 'open' \
             ORDER BY repo_name, number",
        ) {
            Ok(s) => s,
            Err(_) => return BoardData { local_machine, assignments, machines, merge_queue, proposals, ..BoardData::default() },
        };
        let rows = match stmt.query_map([], |row| {
            Ok(OpenIssue {
                repo_name: row.get::<_, String>(0)?,
                number: row.get::<_, i64>(1)? as u64,
                title: row.get::<_, String>(2)?,
            })
        }) {
            Ok(r) => r,
            Err(_) => return BoardData { local_machine, assignments, machines, merge_queue, proposals, ..BoardData::default() },
        };
        rows.filter_map(|r| r.ok()).collect()
    };

    // ── Query board_meta for pipeline config ───────────────────────────────
    let (pipeline_default_gates, pipeline_tracked_labels, pipeline_repos) =
        load_pipeline_meta(&conn);

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
    let mut args: Vec<String> = vec![
        "search".into(),
        "issues".into(),
        "--state".into(),
        "open".into(),
        "--json".into(),
        "number,title,body,labels,repository,url".into(),
        "--limit".into(),
        "100".into(),
    ];
    for label in labels {
        args.push("--label".into());
        args.push(label.clone());
    }
    for (_local, slug) in repos {
        args.push("--repo".into());
        args.push(slug.clone());
    }

    let output = std::process::Command::new("gh")
        .args(&args)
        .output();

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
        Some(a) => a,
        None => return PipelineLoaderResult::Ok(Vec::new()),
    };

    let label_set: std::collections::HashSet<&str> =
        labels.iter().map(|s| s.as_str()).collect();
    let mut issues: Vec<PipelineIssue> = Vec::new();
    for item in arr {
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
        issues.push(PipelineIssue {
            number,
            title,
            body,
            repo_slug,
            coord_repo,
            matched_labels,
        });
    }
    // Stable order: by repo, then by issue number.
    issues.sort_by(|a, b| a.repo_slug.cmp(&b.repo_slug).then(a.number.cmp(&b.number)));
    PipelineLoaderResult::Ok(issues)
}

/// Read pipeline-related entries from the `board_meta` table.
///
/// Returns ``(default_gates, tracked_labels, repos)`` with the documented
/// fallbacks when the keys are missing or unparseable: gates default to
/// ``["review", "merge"]``, tracked labels to ``["coord"]``, and repos to
/// an empty list.  Repos are returned as ``(coord_name, github_slug)``
/// pairs preserving insertion order.
fn load_pipeline_meta(
    conn: &Connection,
) -> (Vec<String>, Vec<String>, Vec<(String, String)>) {
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

    (default_gates, tracked_labels, repos)
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
    /// Which tab is active in the Board detail panel.
    detail_tab: DetailTab,
    /// Scroll offset for the Board Summary detail panel (right side).
    detail_scroll: usize,
    /// Scroll offset for the Board Activity panel.
    /// `None` = auto-scroll to the most-recent entries (default).
    /// `Some(n)` = user has manually scrolled; preserve `n`.
    activity_scroll: Option<usize>,
    /// Scroll offset for the Machine detail panel.
    machine_detail_scroll: usize,
    /// Background command runner for `coord` CLI subcommands.
    command_runner: crate::commands::CommandRunner,
    /// Last time `coord notify` was auto-triggered.
    last_notify: Instant,
    /// Scroll offset for the bottom (command output) panel.
    command_scroll: usize,
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
    /// SidebarSystem listing tracked issues (one section per label).
    pipeline_sidebar: SidebarSystem,
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
    /// Which tab is active in the Pipeline detail pane.
    pipeline_detail_tab: PipelineDetailTab,
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
            detail_tab: DetailTab::default(),
            detail_scroll: 0,
            activity_scroll: None,
            machine_detail_scroll: 0,
            command_runner: crate::commands::CommandRunner::new(),
            last_notify: Instant::now(),
            command_scroll: 0,
            issue_sync_last: None,
            board_search: String::new(),
            board_search_cursor: 0,
            board_search_focused: false,
            board_status_expanded: std::collections::HashMap::new(),
            pipeline_sidebar,
            pipeline_issues: Vec::new(),
            pipeline_sel: None,
            pipeline_loader: None,
            pipeline_last_load: None,
            pipeline_status: None,
            pipeline_detail_tab: PipelineDetailTab::default(),
            pipeline_detail_scroll: 0,
            remote_log_cache: std::cell::RefCell::new(std::collections::HashMap::new()),
            pending_data: Some(start_data_load()),
            fetch_error: None,
            pending_log_fetches: std::cell::RefCell::new(std::collections::HashMap::new()),
        };
        app.rebuild_board_sidebar();
        app.rebuild_pipeline_sidebar();
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
            ],
        )
        .with_status_bar()
        .with_bottom_panel(6.0)
        .with_bottom_panel_limits(3.0, 20.0);
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
                self.rebuild_pipeline_sidebar();
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

        // Inject open issues with no assignment as Pending entries.
        for oi in &self.data.open_issues {
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

        self.has_proposals_section = !self.data.proposals.is_empty();
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

            // Bucket issues by status group; apply filter.
            let mut running: Vec<(usize, &IssueGroup)> = Vec::new();
            let mut failed: Vec<(usize, &IssueGroup)> = Vec::new();
            let mut completed: Vec<(usize, &IssueGroup)> = Vec::new();
            let mut pending: Vec<(usize, &IssueGroup)> = Vec::new();
            for (flat_idx, g) in issues.iter().enumerate() {
                if !issue_matches(g.issue_number, &g.issue_title) {
                    continue;
                }
                match g.status_summary.as_str() {
                    "running" => running.push((flat_idx, g)),
                    "failed" => failed.push((flat_idx, g)),
                    "done" | "merged" => completed.push((flat_idx, g)),
                    _ => pending.push((flat_idx, g)),
                }
            }

            let groups: Vec<(&str, &str, &Vec<(usize, &IssueGroup)>)> = [
                ("Running",   "running",   &running),
                ("Failed",    "failed",    &failed),
                ("Completed", "completed", &completed),
                ("Pending",   "pending",   &pending),
            ]
            .into_iter()
            .filter(|(_, _, v)| !v.is_empty())
            .collect();

            let total: usize = running.len() + failed.len() + completed.len() + pending.len();
            if total > 0 {
                self.board_sidebar.set_section_badge(
                    section_idx,
                    Some(StyledText::plain(format!("({})", total))),
                );
            }

            // Auto-collapse repos with no running/failed issues and no filter.
            let has_active = !running.is_empty() || !failed.is_empty();
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

                let header_color = match *key {
                    "running" => Color::rgb(80, 220, 80),
                    "failed" => Color::rgb(220, 70, 70),
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

    /// Reconstruct the status groups for a repo using the current search filter.
    /// Returns `(flat_idx, &IssueGroup)` vecs ordered Running / Failed / Completed / Pending.
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
        let mut running = Vec::new();
        let mut failed = Vec::new();
        let mut completed = Vec::new();
        let mut pending = Vec::new();
        for (i, g) in flat.iter().enumerate() {
            if !query.is_empty() {
                let num_str = g.issue_number.to_string();
                if !num_str.contains(&query) && !g.issue_title.to_lowercase().contains(&query) {
                    continue;
                }
            }
            match g.status_summary.as_str() {
                "running" => running.push((i, g)),
                "failed" => failed.push((i, g)),
                "done" | "merged" => completed.push((i, g)),
                _ => pending.push((i, g)),
            }
        }
        [("running", running), ("failed", failed), ("completed", completed), ("pending", pending)]
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
        let query = self.board_search.to_lowercase();
        let mut running: Vec<(usize, u64)> = Vec::new();
        let mut failed: Vec<(usize, u64)> = Vec::new();
        let mut completed: Vec<(usize, u64)> = Vec::new();
        let mut pending: Vec<(usize, u64)> = Vec::new();
        for (i, g) in flat.iter().enumerate() {
            if !query.is_empty() {
                let num_str = g.issue_number.to_string();
                if !num_str.contains(&query) && !g.issue_title.to_lowercase().contains(&query) {
                    continue;
                }
            }
            match g.status_summary.as_str() {
                "running" => running.push((i, g.issue_number)),
                "failed" => failed.push((i, g.issue_number)),
                "done" | "merged" => completed.push((i, g.issue_number)),
                _ => pending.push((i, g.issue_number)),
            }
        }
        let groups_ordered: Vec<Vec<(usize, u64)>> = [running, failed, completed, pending]
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

    /// Build the `TabBar` that sits at the top of the Board detail panel.
    fn detail_tab_bar(&self) -> TabBar {
        TabBar {
            id: WidgetId::new("detail-tabs"),
            tabs: vec![
                TabItem {
                    label: " Summary ".to_string(),
                    is_active: self.detail_tab == DetailTab::Summary,
                    is_dirty: false,
                    is_preview: false,
                },
                TabItem {
                    label: " Activity ".to_string(),
                    is_active: self.detail_tab == DetailTab::Activity,
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

        // 3. Cache hit (within 30-second TTL).
        {
            let cache = self.remote_log_cache.borrow();
            if let Some((fetched_at, items)) = cache.get(id) {
                if fetched_at.elapsed() < Duration::from_secs(30) {
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

    /// Activity tab: live feed of worker events parsed from the log file.
    /// Shows the log for the most recent (or running) assignment in the group.
    fn activity_list(&self) -> ListView {
        let (title, items) = match self.board_selected_issue_group() {
            None => (
                " ACTIVITY ".to_string(),
                vec![kv_item("", " No issue selected", None)],
            ),
            Some(group) => {
                // Pick the most interesting assignment: running > failed > last done.
                let best = group
                    .assignments
                    .iter()
                    .find(|a| a.status == "running")
                    .or_else(|| group.assignments.iter().rev().find(|a| a.status == "failed"))
                    .or_else(|| group.assignments.last());
                match best {
                    Some(a) => {
                        let log_items = self.get_activity_log(&a.id, &a.machine);
                        let repo = self.board_active_repo().unwrap_or("?");
                        (
                            format!(" ACTIVITY — {} #{} ", repo, group.issue_number),
                            log_items,
                        )
                    }
                    None => (
                        " ACTIVITY ".to_string(),
                        vec![kv_item("", " No assignment data", None)],
                    ),
                }
            }
        };

        // Scroll to show the most-recent entries (bottom of the list) unless
        // the user has manually scrolled to a specific position.
        let scroll_offset = match self.activity_scroll {
            Some(n) => n,
            None => items.len().saturating_sub(40),
        };

        ListView {
            id: WidgetId::new("activity"),
            title: Some(StyledText::plain(&title)),
            items,
            selected_idx: 0,
            scroll_offset,
            has_focus: false,
            bordered: false,
        }
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

    /// Effective list of stages: always "work" followed by the configured
    /// `pipeline.default_gates` (deduplicated to handle accidental "work"
    /// entries in the gate list).
    fn pipeline_stage_names(&self) -> Vec<String> {
        let mut stages: Vec<String> = vec!["work".to_string()];
        for g in &self.data.pipeline_default_gates {
            if g != "work" {
                stages.push(g.clone());
            }
        }
        stages
    }

    /// Build the SidebarSystem entries for the Pipeline panel. One section
    /// per tracked label, with one row per matching issue.  Re-runs after
    /// every successful `gh` poll.
    fn rebuild_pipeline_sidebar(&mut self) {
        // Preserve selection across rebuilds by (repo_slug, issue#).
        let prev_sel = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))
            .map(|i| (i.repo_slug.clone(), i.number));

        // Build one section per label (so empty-label apps still render
        // a useful sidebar).
        let mut defs: Vec<SidebarSectionDef> = Vec::new();
        for label in &self.data.pipeline_tracked_labels {
            let mut def = SidebarSectionDef::new(
                format!("section:label:{}", label),
                label.clone(),
            );
            def.show_chevron = true;
            def.size = SectionSize::Content;
            defs.push(def);
        }

        let mut sidebar = SidebarSystem::new(defs);
        sidebar.set_navigation_mode(NavigationMode::Selection);
        sidebar.set_allow_collapse(true);
        sidebar.set_scroll_mode(ScrollMode::WholePanel);

        // Group issues by their first matched label (a single issue with
        // multiple matched labels appears once, under its first match).
        for (sec_idx, label) in self.data.pipeline_tracked_labels.iter().enumerate() {
            let mut rows: Vec<TreeRow> = Vec::new();
            let mut count = 0usize;
            for (i, issue) in self.pipeline_issues.iter().enumerate() {
                if issue
                    .matched_labels
                    .first()
                    .map(|l| l == label)
                    .unwrap_or(false)
                {
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
                            StyledSpan::with_fg(trunc(&issue.title, 22), title_color),
                        ],
                    };
                    rows.push(TreeRow {
                        path: vec![count as u16],
                        indent: 0,
                        icon: None,
                        text,
                        badge: Some(Badge::colored(&badge_text, badge_color)),
                        is_expanded: None,
                        decoration: Decoration::Normal,
                        edit: None,
                    });
                    let _ = i;
                    count += 1;
                }
            }
            if count > 0 {
                sidebar.set_section_badge(
                    sec_idx,
                    Some(StyledText::plain(format!("({})", count))),
                );
            }
            sidebar.set_rows(sec_idx, rows);
        }

        // Default-select the first label section that has at least one issue.
        if sidebar.active_section().is_none() {
            for (i, label) in self.data.pipeline_tracked_labels.iter().enumerate() {
                let has_any = self.pipeline_issues.iter().any(|issue| {
                    issue
                        .matched_labels
                        .first()
                        .map(|l| l == label)
                        .unwrap_or(false)
                });
                if has_any {
                    sidebar.set_active_section(Some(i));
                    sidebar.set_selected_path(i, Some(vec![0]));
                    break;
                }
            }
        }

        self.pipeline_sidebar = sidebar;

        // Restore previous selection if the issue still exists.
        if let Some((repo, num)) = prev_sel {
            'outer: for (sec_idx, label) in self.data.pipeline_tracked_labels.iter().enumerate() {
                let mut row = 0u16;
                for (i, issue) in self.pipeline_issues.iter().enumerate() {
                    if issue.matched_labels.first().map(|l| l == label).unwrap_or(false) {
                        if issue.repo_slug == repo && issue.number == num {
                            self.pipeline_sel = Some(i);
                            self.pipeline_sidebar.set_active_section(Some(sec_idx));
                            self.pipeline_sidebar.set_selected_path(sec_idx, Some(vec![row]));
                            break 'outer;
                        }
                        row += 1;
                    }
                }
            }
        }
        // Otherwise sync `pipeline_sel` to the sidebar's default selection.
        self.pipeline_sel = self.selected_pipeline_index();
    }

    /// Resolve the SidebarSystem's current selection to a `pipeline_issues`
    /// index.  Returns `None` when nothing is selected or the selection
    /// points past the end (can happen after rebuild + label re-grouping).
    fn selected_pipeline_index(&self) -> Option<usize> {
        let section = self.pipeline_sidebar.active_section()?;
        let label = self.data.pipeline_tracked_labels.get(section)?;
        let path = self.pipeline_sidebar.selected_path(section)?;
        let row = *path.first()? as usize;
        let mut count = 0usize;
        for (i, issue) in self.pipeline_issues.iter().enumerate() {
            if issue
                .matched_labels
                .first()
                .map(|l| l == label)
                .unwrap_or(false)
            {
                if count == row {
                    return Some(i);
                }
                count += 1;
            }
        }
        None
    }

    /// Resolve the per-stage status of an issue from existing assignments.
    ///
    /// "work" is the first stage and matches assignments with
    /// `assignment_type` `None` or `"work"`.  Other stage names match
    /// assignments by exact `assignment_type`.
    fn stage_status_for(
        &self,
        issue: &PipelineIssue,
        stage: &str,
    ) -> StageStatus {
        // Collect assignments for this issue (matching repo or repo_slug).
        let related: Vec<&Assignment> = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| {
                if let Some(local) = &issue.coord_repo {
                    a.repo == *local
                } else {
                    // No local mapping known — match by issue # alone.
                    true
                }
            })
            .collect();

        let stage_match = |a: &&Assignment| -> bool {
            let t = a.assignment_type.as_deref().unwrap_or("work");
            if stage == "work" {
                t == "work" || t == "plan"
            } else {
                t == stage
            }
        };

        // Most recent first (assignments are pre-sorted by dispatched_at desc).
        let matching: Vec<&Assignment> = related.into_iter().filter(stage_match).collect();
        if matching.iter().any(|a| a.status == "running") {
            return StageStatus::Active;
        }
        if let Some(latest) = matching.first() {
            match latest.status.as_str() {
                "done" => return StageStatus::Done,
                "failed" => return StageStatus::Failed,
                _ => {}
            }
        }
        StageStatus::Pending
    }

    /// Returns the *display* current stage for the sidebar badge — the
    /// first non-Done stage, or "merged" once every stage is Done.
    fn derive_current_stage(&self, issue: &PipelineIssue) -> String {
        let stages = self.pipeline_stage_names();
        for s in &stages {
            let st = self.stage_status_for(issue, s);
            if st != StageStatus::Done {
                return s.clone();
            }
        }
        "done".to_string()
    }

    /// Build the quadraui `PipelineView` widget for the selected issue.
    fn build_pipeline_widget(&self) -> Option<QuiPipelineView> {
        let idx = self.pipeline_sel?;
        let issue = self.pipeline_issues.get(idx)?;
        let stage_names = self.pipeline_stage_names();

        // The Go button goes on the first Pending stage that we know how
        // to dispatch — currently the "work" stage only (other gates are
        // dispatched implicitly by the coordinator after work completes).
        let mut go_attached = false;
        let stages: Vec<QuiPipelineStage> = stage_names
            .iter()
            .map(|name| {
                let status = self.stage_status_for(issue, name);
                let label = match name.as_str() {
                    "work" => "Work".to_string(),
                    other => {
                        let mut s = other.to_string();
                        if let Some(c) = s.get_mut(0..1) {
                            c.make_ascii_uppercase();
                        }
                        s
                    }
                };
                let action = if !go_attached
                    && status == StageStatus::Pending
                    && name == "work"
                    && issue.coord_repo.is_some()
                {
                    go_attached = true;
                    Some("Go".to_string())
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

        Some(QuiPipelineView {
            id: WidgetId::new("pipeline:detail"),
            stages,
            focused_stage: None,
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

    /// Dispatch the "Go" action for the currently selected issue. Spawns
    /// `coord assign <machine> <repo> <issue>` via the existing command
    /// runner. Returns `true` if a command was spawned, `false` if we
    /// fell back to a queue-style status message.
    fn dispatch_pipeline_go(&mut self) -> bool {
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
        let spawned = self.command_runner.spawn(&[
            "assign",
            &machine_name,
            &coord_repo,
            &issue_str,
        ]);
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

    /// Kick off a background `gh search issues` poll (no-op if one is
    /// already in flight or we polled less than 15 s ago).
    fn maybe_kick_pipeline_loader(&mut self) {
        if self.pipeline_loader.is_some() {
            return;
        }
        if let Some(t) = self.pipeline_last_load {
            if t.elapsed() < Duration::from_secs(15) {
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

    /// Drain the in-flight poll channel without blocking. Returns `true`
    /// when results were received and the issue list/sidebar changed.
    fn poll_pipeline_loader(&mut self) -> bool {
        let rx = match &self.pipeline_loader {
            Some(rx) => rx,
            None => return false,
        };
        match rx.try_recv() {
            Ok(PipelineLoaderResult::Ok(issues)) => {
                self.pipeline_issues = issues;
                self.pipeline_loader = None;
                self.rebuild_pipeline_sidebar();
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
        let mut items: Vec<ListItem> = Vec::new();
        if let Some(idx) = self.pipeline_sel {
            if let Some(issue) = self.pipeline_issues.get(idx) {
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![
                            StyledSpan::with_fg(format!(" #{}", issue.number), Color::rgb(150, 150, 240)),
                            StyledSpan::with_fg(format!("  {}", issue.title), Color::rgb(230, 230, 255)),
                        ],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                });
                items.push(kv_item("", "", None));
                if issue.body.is_empty() {
                    items.push(kv_item("", " (no description)", Some(Color::rgb(100, 100, 100))));
                } else {
                    for line in issue.body.lines() {
                        items.push(kv_item("", &format!(" {}", line), Some(Color::rgb(200, 200, 210))));
                    }
                }
            }
        } else {
            items.push(kv_item("", " No issue selected", Some(Color::rgb(100, 100, 100))));
        }
        ListView {
            id: WidgetId::new("pipeline-issue-body"),
            title: None,
            items,
            selected_idx: 0,
            scroll_offset: self.pipeline_detail_scroll,
            has_focus: false,
            bordered: false,
        }
    }

    /// Pipeline tab: meta strip (repo/labels/gates/status).
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
                items.push(kv_item("Gates", &self.pipeline_stage_names().join(" → "), Some(Color::rgb(160, 160, 180))));
                if let Some((msg, when)) = &self.pipeline_status {
                    if when.elapsed() < Duration::from_secs(8) {
                        items.push(kv_item("", "", None));
                        items.push(kv_item("", &format!("  {}", msg), Some(Color::rgb(180, 180, 100))));
                    }
                }
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


    // ── Bottom panel: command output ──────────────────────────────────────

    fn command_output_list(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();

        // Persistent warning when coordinator.yml could not be located at
        // startup. The commands panel is shown regardless so the user can see
        // the message even before pressing any key.
        if self.command_runner.config_path.is_none() {
            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(
                        " coordinator.yml not found — run coord-tui from your project directory ",
                        Color::rgb(220, 100, 60),
                    )],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Error,
            });
        }

        if let Some((label, elapsed)) = self.command_runner.running_info() {
            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(
                        format!(" {} ({:.0}s)... ", label, elapsed.as_secs_f64()),
                        Color::rgb(255, 220, 100),
                    )],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Normal,
            });
        } else if let Some(result) = self.command_runner.last_result() {
            let color = if result.exit_code == 0 {
                Color::rgb(120, 200, 120)
            } else {
                Color::rgb(220, 100, 100)
            };
            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(
                        format!(
                            " {} (exit {}, {:.1}s) ",
                            result.label, result.exit_code, result.duration.as_secs_f64()
                        ),
                        color,
                    )],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Header,
            });
            for line in result.stdout.lines().take(50) {
                items.push(kv_item("", &format!(" {}", line), None));
            }
            if !result.stderr.is_empty() {
                for line in result.stderr.lines().take(20) {
                    items.push(kv_item("", &format!(" {}", line), Some(Color::rgb(220, 100, 100))));
                }
            }
        } else {
            items.push(kv_item(
                "",
                " No commands run yet. p=plan  n=notify  a=approve  m=merge",
                Some(Color::rgb(100, 100, 120)),
            ));
        }

        ListView {
            id: WidgetId::new("command-output"),
            title: Some(StyledText::plain(" COMMANDS ")),
            items,
            selected_idx: 0,
            scroll_offset: self.command_scroll,
            has_focus: false,
            bordered: false,
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
                            self.activity_scroll = None;
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
                            self.activity_scroll = None;
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
        }
    }

    /// Click in the main panel — in Board view this handles the tab bar
    /// (first row of the panel).  In Pipeline view this hit-tests the
    /// PipelineView primitive and dispatches the "Go" action.
    fn mouse_main_click(&mut self, pos: Point, main_b: Rect, lh: f32) -> bool {
        if self.active_view == SidebarView::Pipeline {
            if let Some(view) = self.build_pipeline_widget() {
                let pv_rect = pipeline_detail_pv_rect(main_b, lh);
                let layout = tui_pipeline_layout(&view, pv_rect);
                match layout.hit_test(pos.x, pos.y) {
                    PipelineHit::Action(_) => {
                        self.dispatch_pipeline_go();
                        return true;
                    }
                    PipelineHit::Body(_) | PipelineHit::Empty => return false,
                }
            }
            return false;
        }
        if self.active_view != SidebarView::Board {
            return false;
        }
        // The tab bar occupies the first `lh` pixels/cells of the panel.
        if pos.y - main_b.y < lh {
            // " Summary " is 9 chars; " Activity " is 10 chars (compact tabs,
            // no separator). Anything in the first 9 columns → Summary tab.
            let x_off = pos.x - main_b.x;
            let new_tab = if x_off < 9.0 {
                DetailTab::Summary
            } else {
                DetailTab::Activity
            };
            if new_tab != self.detail_tab {
                self.detail_tab = new_tab;
                return true;
            }
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
        }
    }

    /// Scroll wheel in the main panel (detail / activity / machine detail).
    fn mouse_main_scroll(&mut self, delta: ScrollDelta, main_b: Rect, lh: f32) -> bool {
        let visible = content_visible_rows(main_b, lh);
        match self.active_view {
            SidebarView::Board => {
                match self.detail_tab {
                    DetailTab::Summary => {
                        let items = self.detail_list().items.len();
                        let max = items.saturating_sub(visible.saturating_sub(1));
                        if delta.y > 0.0 {
                            self.detail_scroll = self.detail_scroll.saturating_sub(1);
                        } else if delta.y < 0.0 {
                            self.detail_scroll = (self.detail_scroll + 1).min(max);
                        }
                    }
                    DetailTab::Activity => {
                        let list = self.activity_list();
                        let items = list.items.len();
                        let current = self.activity_scroll.unwrap_or(list.scroll_offset);
                        let max = items.saturating_sub(visible.saturating_sub(1));
                        if delta.y > 0.0 {
                            self.activity_scroll = Some(current.saturating_sub(1));
                        } else if delta.y < 0.0 {
                            self.activity_scroll = Some((current + 1).min(max));
                        }
                    }
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
                // The Pipeline detail pane has no scrollable region today —
                // the issue summary fits in a fixed strip. Consume the event
                // anyway so it doesn't propagate further.
                let _ = visible;
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
            StatusBarSegment {
                text: format!(" {} ", view_label),
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
        let hints = if proposals > 0 {
            format!(" p=plan  a=approve({})  m=merge  R=retry  q=quit ", proposals)
        } else {
            " p=plan  n=notify  m=merge  R=retry  q=quit ".to_string()
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
        if let Some(sidebar_rect) = layout.sidebar_content_bounds {
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
            }
        }

        // ── Main: detail panel only (full main_content_bounds) ───────
        let m = layout.main_content_bounds;
        match self.active_view {
            SidebarView::Board => {
                // Tab bar (1 line) + tab content below.
                let tab_h = lh;
                let tab_rect = Rect::new(m.x, m.y, m.width, tab_h);
                let content_rect =
                    Rect::new(m.x, m.y + tab_h, m.width, (m.height - tab_h).max(0.0));
                backend.draw_tab_bar(tab_rect, &self.detail_tab_bar(), None);
                match self.detail_tab {
                    DetailTab::Summary => {
                        backend.draw_list(content_rect, &self.detail_list());
                    }
                    DetailTab::Activity => {
                        backend.draw_list(content_rect, &self.activity_list());
                    }
                }
            }
            SidebarView::Machines => {
                backend.draw_list(m, &self.machine_detail_list());
            }
            SidebarView::Pipeline => {
                if self.pipeline_sel.is_none() && self.pipeline_issues.is_empty() {
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
                    }
                }
            }
        }

        // ── Bottom panel: command output ─────────────────────────────────
        if let Some(bp) = layout.bottom_panel_bounds {
            backend.draw_list(bp, &self.command_output_list());
        }
    }

    fn handle(&mut self, event: UiEvent, backend: &mut dyn Backend, ctx: &ShellContext) -> Reaction {
        let mut needs_redraw = false;

        // ── Drain pending background data load ──────────────────────────
        if self.apply_pending_data() {
            needs_redraw = true;
        }

        // ── Auto-refresh: kick off background load when interval elapses ─
        // (ShellApp has no tick(); check elapsed on every UI event.)
        if self.refreshed_at.elapsed() >= REFRESH_EVERY && self.pending_data.is_none() {
            self.pending_data = Some(start_data_load());
            if self.active_view == SidebarView::Pipeline {
                self.maybe_kick_pipeline_loader();
            }
            needs_redraw = true;
        }

        // ── Poll background command runner ──────────────────────────────
        if self.command_runner.poll() {
            self.refresh();
            needs_redraw = true;
        }

        // ── Poll background gh issue loader ─────────────────────────────
        if self.poll_pipeline_loader() {
            needs_redraw = true;
        }

        // ── Auto-notify: run `coord notify` when running assignments exist ─
        let has_running = self.data.assignments.iter().any(|a| a.status == "running");
        if has_running
            && self.last_notify.elapsed() >= NOTIFY_EVERY
            && !self.command_runner.is_running()
        {
            self.command_runner.spawn(&["notify"]);
            self.last_notify = Instant::now();
        }

        // ── Mouse / scroll dispatch (before consuming the event) ─────────────
        needs_redraw |= self.handle_mouse(&event, backend, ctx);

        // ── Pre-compute panel bounds for keyboard visible-row estimates ───────
        let list_b = ctx.sidebar_bounds().unwrap_or(ctx.main_bounds());
        let lh = backend.line_height();

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
                                self.activity_scroll = None;
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
                                        self.activity_scroll = None;
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
                                        self.activity_scroll = None;
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
                        }
                        needs_redraw = true;
                    }

                    // ── h/l — switch Pipeline detail tabs ────────────────
                    Key::Char('h') | Key::Named(NamedKey::Left)
                        if self.active_view == SidebarView::Pipeline =>
                    {
                        self.pipeline_detail_tab = PipelineDetailTab::Pipeline;
                        self.pipeline_detail_scroll = 0;
                        needs_redraw = true;
                    }
                    Key::Char('l') | Key::Named(NamedKey::Right)
                        if self.active_view == SidebarView::Pipeline =>
                    {
                        self.pipeline_detail_tab = PipelineDetailTab::Issue;
                        self.pipeline_detail_scroll = 0;
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
                                    self.activity_scroll = None;
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
                                    self.activity_scroll = None;
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
                        }
                        needs_redraw = true;
                    }

                    // ── Enter — fire Go for selected Pipeline issue ──────
                    Key::Named(NamedKey::Enter)
                        if self.active_view == SidebarView::Pipeline =>
                    {
                        self.dispatch_pipeline_go();
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
                            self.activity_scroll = None;
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
                            self.activity_scroll = None;
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

                    // ── Left / h — switch to Summary tab (Board only) ─────
                    Key::Named(NamedKey::Left) | Key::Char('h')
                        if self.active_view == SidebarView::Board
                            && !self.board_search_focused =>
                    {
                        if self.detail_tab != DetailTab::Summary {
                            self.detail_tab = DetailTab::Summary;
                            needs_redraw = true;
                        }
                    }

                    // ── Right / l — switch to Activity tab (Board only) ───
                    Key::Named(NamedKey::Right) | Key::Char('l')
                        if self.active_view == SidebarView::Board
                            && !self.board_search_focused =>
                    {
                        if self.detail_tab != DetailTab::Activity {
                            self.detail_tab = DetailTab::Activity;
                            needs_redraw = true;
                        }
                    }

                    Key::Char('r') => {
                        self.refresh();
                        self.kick_issue_sync();
                        needs_redraw = true;
                    }

                    // ── Coordinator commands ─────────────────────────────
                    Key::Char('p') => {
                        self.command_runner.spawn(&["plan"]);
                        needs_redraw = true;
                    }
                    Key::Char('n') => {
                        self.command_runner.spawn(&["notify"]);
                        self.last_notify = Instant::now();
                        needs_redraw = true;
                    }
                    Key::Char('a') => {
                        if let Some(proposal) = self.board_selected_proposal() {
                            let id_str = proposal.id.to_string();
                            self.command_runner.spawn(&["approve", &id_str]);
                            needs_redraw = true;
                        }
                    }
                    Key::Char('A') => {
                        if !self.data.proposals.is_empty() {
                            let ids: Vec<String> =
                                self.data.proposals.iter().map(|p| p.id.to_string()).collect();
                            let joined = ids.join(",");
                            self.command_runner.spawn(&["approve", &joined]);
                            needs_redraw = true;
                        }
                    }
                    Key::Char('m') => {
                        self.command_runner.spawn(&["merge"]);
                        needs_redraw = true;
                    }
                    Key::Char('R') => {
                        if let Some(a) = self.board_selected_failed_assignment() {
                            let id = a.id.clone();
                            self.command_runner.spawn(&["retry", &id]);
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
                _ => return,
            };
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

// ─── Unit tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

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
            detail_tab: DetailTab::default(),
            detail_scroll: 0,
            activity_scroll: None,
            machine_detail_scroll: 0,
            command_runner: crate::commands::CommandRunner::new(),
            last_notify: Instant::now(),
            command_scroll: 0,
            issue_sync_last: None,
            board_search: String::new(),
            board_search_cursor: 0,
            board_search_focused: false,
            board_status_expanded: std::collections::HashMap::new(),
            pipeline_sidebar,
            pipeline_issues: Vec::new(),
            pipeline_sel: None,
            pipeline_loader: None,
            pipeline_last_load: None,
            pipeline_status: None,
            pipeline_detail_tab: PipelineDetailTab::default(),
            pipeline_detail_scroll: 0,
            remote_log_cache: std::cell::RefCell::new(std::collections::HashMap::new()),
            pending_data: None,
            fetch_error: None,
            pending_log_fetches: std::cell::RefCell::new(std::collections::HashMap::new()),
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

    // ── DetailTab ─────────────────────────────────────────────────────────────

    #[test]
    fn detail_tab_default_is_summary() {
        assert_eq!(DetailTab::default(), DetailTab::Summary);
    }

    #[test]
    fn detail_tab_summary_active_in_new_app() {
        let app = make_app_default();
        assert_eq!(app.detail_tab, DetailTab::Summary);
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

    #[test]
    fn mouse_main_click_tab_summary() {
        let mut app = make_app_default();
        app.detail_tab = DetailTab::Activity;
        let main_b = Rect::new(50.0, 0.0, 40.0, 40.0);
        // Click at x=51 → offset 1 < 9 → Summary tab.
        let changed = app.mouse_main_click(Point::new(51.0, 0.0), main_b, 1.0);
        assert!(changed);
        assert_eq!(app.detail_tab, DetailTab::Summary);
    }

    #[test]
    fn mouse_main_click_tab_activity() {
        let mut app = make_app_default();
        assert_eq!(app.detail_tab, DetailTab::Summary);
        let main_b = Rect::new(50.0, 0.0, 40.0, 40.0);
        // Click at x=60 → offset 10 ≥ 9 → Activity tab.
        let changed = app.mouse_main_click(Point::new(60.0, 0.0), main_b, 1.0);
        assert!(changed);
        assert_eq!(app.detail_tab, DetailTab::Activity);
    }

    #[test]
    fn mouse_main_click_below_tab_row_is_ignored() {
        let mut app = make_app_default();
        let main_b = Rect::new(50.0, 0.0, 40.0, 40.0);
        // Click at row y=2, well below the tab bar at y=0.
        let changed = app.mouse_main_click(Point::new(55.0, 2.0), main_b, 1.0);
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
    fn activity_scroll_none_means_auto() {
        let app = make_app_default();
        assert!(app.activity_scroll.is_none());
    }

    #[test]
    fn activity_scroll_manual_value_preserved() {
        let mut app = make_app_default();
        app.activity_scroll = Some(7);
        app.refresh();
        assert_eq!(app.activity_scroll, Some(7));
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

    #[test]
    fn board_status_groups_order_running_failed_completed_pending() {
        let assignments = vec![
            make_assignment_typed("done", 1, "repo-a", Some("work")),
            make_assignment_typed("running", 2, "repo-a", Some("work")),
            make_assignment_typed("failed", 3, "repo-a", Some("work")),
        ];
        let app = make_app_with_assignments(assignments);
        let cache = app.board_issues_cache.clone();
        let groups = app.board_grouped_for_repo(&cache, "repo-a");
        assert_eq!(groups.len(), 3);
        assert_eq!(groups[0].0, "running");
        assert_eq!(groups[1].0, "failed");
        assert_eq!(groups[2].0, "completed");
    }

    #[test]
    fn board_empty_status_groups_are_hidden() {
        let assignments = vec![make_assignment_typed("running", 10, "repo-a", Some("work"))];
        let app = make_app_with_assignments(assignments);
        let cache = app.board_issues_cache.clone();
        let groups = app.board_grouped_for_repo(&cache, "repo-a");
        assert_eq!(groups.len(), 1);
        assert_eq!(groups[0].0, "running");
    }

    #[test]
    fn board_select_issue_uses_two_level_path() {
        let assignments = vec![
            make_assignment_typed("running", 10, "repo-a", Some("work")),
            make_assignment_typed("done", 20, "repo-a", Some("work")),
        ];
        let mut app = make_app_with_assignments(assignments);
        app.select_issue("repo-a", 20);
        // Issue #20 is "done" → Completed group (index 1 after Running), issue index 0.
        let path = app.board_sidebar.selected_path(1).cloned();
        assert_eq!(path, Some(vec![1u16, 0u16]), "done issue should be at [1,0]");
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
        app.pipeline_issues = vec![
            PipelineIssue {
                number: 42,
                title: "Add cool thing".to_string(),
                body: String::new(),
                repo_slug: "acme/api".to_string(),
                coord_repo: Some("api".to_string()),
                matched_labels: vec!["coord".to_string()],
            },
            PipelineIssue {
                number: 99,
                title: "Mystery repo issue".to_string(),
                body: String::new(),
                repo_slug: "other/repo".to_string(),
                coord_repo: None,
                matched_labels: vec!["coord".to_string()],
            },
        ];
        app.rebuild_pipeline_sidebar();
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
    fn rebuild_pipeline_sidebar_groups_issues_by_label() {
        let app = make_pipeline_app();
        // One section per tracked label.
        assert_eq!(app.data.pipeline_tracked_labels.len(), 1);
        // Two issues both have label "coord" → both appear in section 0.
        assert!(app.pipeline_issues.len() == 2);
    }

    #[test]
    fn rebuild_pipeline_sidebar_default_selects_first_issue() {
        let app = make_pipeline_app();
        // Default selection should be the first issue under the first label.
        assert!(app.pipeline_sel.is_some());
        assert_eq!(app.pipeline_sel.unwrap(), 0);
    }

    #[test]
    fn stage_status_for_pending_when_no_assignment_exists() {
        let app = make_pipeline_app();
        let issue = &app.pipeline_issues[0];
        assert_eq!(app.stage_status_for(issue, "work"), StageStatus::Pending);
        assert_eq!(app.stage_status_for(issue, "review"), StageStatus::Pending);
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
        let (gates, labels, repos) = load_pipeline_meta(&conn);
        assert_eq!(gates, vec!["review".to_string(), "merge".to_string()]);
        assert_eq!(labels, vec!["coord".to_string()]);
        assert!(repos.is_empty());
    }

    #[test]
    fn pipeline_loader_reads_persisted_values() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE board_meta (key TEXT PRIMARY KEY, value TEXT);
             INSERT INTO board_meta VALUES \
              ('pipeline_default_gates', '[\"plan\",\"work\",\"smoke\"]'), \
              ('pipeline_tracked_labels', '[\"hotfix\",\"feature\"]'), \
              ('pipeline_repos', '{\"api\":\"acme/api\"}');",
        )
        .unwrap();
        let (gates, labels, repos) = load_pipeline_meta(&conn);
        assert_eq!(gates, vec!["plan", "work", "smoke"]);
        assert_eq!(labels, vec!["hotfix", "feature"]);
        assert_eq!(repos, vec![("api".to_string(), "acme/api".to_string())]);
    }

}
