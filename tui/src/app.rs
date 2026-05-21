//! Backend-neutral app logic for coord-tui.
//!
//! [`CoordApp`] implements [`quadraui::AppLogic`] using only the
//! backend-neutral trait surface (`draw_list`, `draw_split`, `draw_tree`,
//! `draw_status_bar`). No ratatui or crossterm symbols appear here —
//! those live exclusively in the TUI and GTK shim entry points.
//!
//! ## Layout
//!
//! ```text
//! ┌────────────┬────────────────────────────┬──────────────────────────┐
//! │ VIEWS      │ BOARD (Board view)          │ DETAIL                  │
//! │ ▶ Board    │ ▼ Running (1)               │ claude-coordinator #115  │
//! │   Machines │   #115  claude-coord  RUN   │  ID     6b2670e…        │
//! │            │ ▼ Failed (0)                │  Machine dellserver      │
//! │            │ ▶ Done (3)                  │                         │
//! ├────────────┼────────────────────────────┼──────────────────────────┤
//! │            │ MACHINES (Machines view)    │ DETAIL — dellserver     │
//! │   Board    │ ● dellserver (local)  1     │  Status  reachable      │
//! │ ▶ Machines │ ○ elitebook         idle    │  JOB HISTORY            │
//! └────────────┴────────────────────────────┴──────────────────────────┘
//! │ coord-tui  Board  ↻ 3s  1=Board 2=Machines Tab=switch j/k=nav Enter/Space=expand │
//! ```
//!
//! **Data sources:**
//! - `~/.coord/coord.db` — SQLite database (WAL mode) written by the coordinator
//!
//! **Auto-refresh:** every 5 s via [`AppLogic::tick`], which quadraui
//! calls after every event batch (including empty timeout batches).

use std::net::{TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use rusqlite::{Connection, OpenFlags};

use quadraui::{
    AppLogic, Backend, Badge, Color, Decoration, Key, ListItem, ListView, MouseButton, NamedKey,
    Point, Reaction, Rect, ScrollDelta, Split, SplitDirection, SplitMeasure, StatusBar,
    StatusBarSegment, StyledSpan, StyledText, TabBar, TabItem, TreeController,
    TreeControllerEvent, TreeRow, UiEvent, WidgetId,
};

// ─── Auto-refresh interval ────────────────────────────────────────────────────

/// Reload board data every 5 seconds.
const REFRESH_EVERY: Duration = Duration::from_secs(5);

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

// ─── Sidebar views ────────────────────────────────────────────────────────────

/// The selectable top-level views shown in the left sidebar.
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
enum SidebarView {
    #[default]
    Board,
    Machines,
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

    fn index(self) -> usize {
        match self {
            SidebarView::Board => 0,
            SidebarView::Machines => 1,
            SidebarView::Pipeline => 2,
        }
    }

    /// Cycle to the next view (wraps around).
    fn next(self) -> Self {
        match self {
            SidebarView::Board => SidebarView::Machines,
            SidebarView::Machines => SidebarView::Pipeline,
            SidebarView::Pipeline => SidebarView::Board,
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
    reachable: bool,
    active_count: usize,
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

#[derive(Default)]
struct BoardData {
    local_machine: String,
    assignments: Vec<Assignment>,
    machines: Vec<Machine>,
    merge_queue: Vec<MergeQueueEntry>,
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

/// Read `~/.coord/logs/<id>.log` and return displayable `ListItem`s.
///
/// Handles both stream-json logs (NDJSON from `claude -p --output-format
/// stream-json`) and plain-text logs (stdout of older workers). If the log
/// file doesn't exist locally, returns a single "remote assignment" notice.
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

    // ── Query machines (name = nickname, host = Tailscale FQDN) ───────────
    let machine_rows: Vec<(String, String)> = {
        let mut stmt = match conn.prepare("SELECT name, host FROM machines") {
            Ok(s) => s,
            Err(_) => {
                return BoardData {
                    assignments,
                    ..BoardData::default()
                }
            }
        };
        let rows = match stmt.query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
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
    let probes: Vec<(String, std::sync::mpsc::Receiver<bool>)> = machine_rows
        .iter()
        .map(|(name, host)| {
            use std::sync::mpsc;
            let h = host.clone();
            let (tx, rx) = mpsc::channel();
            std::thread::spawn(move || {
                let _ = tx.send(tcp_probe(&h, 7433));
            });
            (name.clone(), rx)
        })
        .collect();

    let machines: Vec<Machine> = probes
        .into_iter()
        .map(|(name, rx)| {
            let reachable = rx.recv_timeout(Duration::from_millis(250)).unwrap_or(false);
            let active_count = assignments
                .iter()
                .filter(|a| a.machine == name && a.status == "running")
                .count();
            Machine {
                name,
                reachable,
                active_count,
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
        .find(|(_, host)| *host == local_hostname)
        .map(|(name, _)| name.clone())
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
                    merge_queue: Vec::new(),
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
                    merge_queue: Vec::new(),
                };
            }
        };
        rows.filter_map(|r| r.ok()).collect()
    };

    BoardData {
        local_machine,
        assignments,
        machines,
        merge_queue,
    }
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
    /// Tree controller for the Board view (selection, scroll, vim-keys).
    board_tree: TreeController,
    /// Expand/collapse state for each status group: [Running, Failed, Done].
    /// Running and Failed are expanded by default; Done is collapsed.
    board_groups_expanded: [bool; 3],
    /// Selected machine index in the Machines view.
    machine_sel: usize,
    /// Scroll offset for the machines list.
    machine_scroll: usize,
    refreshed_at: Instant,
    /// Which tab is active in the Board detail panel.
    detail_tab: DetailTab,
    /// Selected issue index in the Pipeline view.
    pipeline_sel: usize,
    /// Scroll offset for the Pipeline issue list.
    pipeline_scroll: usize,
    /// Scroll offset for the Board Summary detail panel (right side).
    detail_scroll: usize,
    /// Scroll offset for the Board Activity panel.
    /// `None` = auto-scroll to the most-recent entries (default).
    /// `Some(n)` = user has manually scrolled; preserve `n`.
    activity_scroll: Option<usize>,
    /// Scroll offset for the Machine detail panel.
    machine_detail_scroll: usize,
    /// Scroll offset for the Pipeline detail panel.
    pipeline_detail_scroll: usize,
}

impl Default for CoordApp {
    fn default() -> Self {
        Self::new()
    }
}

impl CoordApp {
    /// Create a new app, loading initial board data from the SQLite DB.
    pub fn new() -> Self {
        let data = load_data();
        let mut app = Self {
            data,
            active_view: SidebarView::default(),
            board_tree: TreeController::new("board"),
            board_groups_expanded: [true, true, false],
            machine_sel: 0,
            machine_scroll: 0,
            refreshed_at: Instant::now(),
            detail_tab: DetailTab::default(),
            pipeline_sel: 0,
            pipeline_scroll: 0,
            detail_scroll: 0,
            activity_scroll: None,
            machine_detail_scroll: 0,
            pipeline_detail_scroll: 0,
        };
        app.rebuild_board_tree_rows();
        app
    }

    fn refresh(&mut self) {
        self.data = load_data();
        self.refreshed_at = Instant::now();
        let m = self.data.machines.len();
        if m > 0 {
            self.machine_sel = self.machine_sel.min(m - 1);
        } else {
            self.machine_sel = 0;
        }
        let p = self.pipeline_issues().len();
        if p > 0 {
            self.pipeline_sel = self.pipeline_sel.min(p - 1);
        } else {
            self.pipeline_sel = 0;
        }
        self.rebuild_board_tree_rows();
    }

    /// Rebuild the tree rows from current data + expansion state, then push
    /// them into the `TreeController`. Clears the selection if it no longer
    /// exists in the new row set (e.g. after collapsing a group).
    fn rebuild_board_tree_rows(&mut self) {
        let rows = self.build_board_rows();
        // Clear selection if its path vanished (e.g. group was collapsed).
        if let Some(path) = self.board_tree.selected_path().cloned() {
            if !rows.iter().any(|r| r.path == path) {
                self.board_tree.set_selected_path(None);
            }
        }
        self.board_tree.set_rows(rows);
    }

    /// Build the flat-ordered [`TreeRow`] list from current assignments and
    /// group expansion state.  The assignments vector is already sorted
    /// running → failed → done, so group offsets are trivially computed.
    fn build_board_rows(&self) -> Vec<TreeRow> {
        let n_running = self
            .data
            .assignments
            .iter()
            .filter(|a| a.status == "running")
            .count();
        let n_failed = self
            .data
            .assignments
            .iter()
            .filter(|a| a.status == "failed")
            .count();
        let n_done = self.data.assignments.len() - n_running - n_failed;

        // (label, count, color, start-index-into-assignments)
        let groups: [(&str, usize, Color, usize); 3] = [
            ("Running", n_running, Color::rgb(80, 220, 80), 0),
            (
                "Failed",
                n_failed,
                Color::rgb(220, 70, 70),
                n_running,
            ),
            (
                "Done",
                n_done,
                Color::rgb(120, 120, 120),
                n_running + n_failed,
            ),
        ];

        let mut rows = Vec::new();

        for (g_idx, (label, count, color, start)) in groups.iter().enumerate() {
            let expanded = self.board_groups_expanded[g_idx];

            // ── Group header ──────────────────────────────────────────
            let header_text = StyledText {
                spans: vec![StyledSpan::with_fg(
                    format!("{} ({})", label, count),
                    *color,
                )],
            };
            rows.push(TreeRow {
                path: vec![g_idx as u16],
                indent: 0,
                icon: None,
                text: header_text,
                badge: None,
                is_expanded: Some(expanded),
                decoration: Decoration::Normal,
                edit: None,
            });

            if !expanded {
                continue;
            }

            // ── Assignment leaves ─────────────────────────────────────
            for i in 0..*count {
                let a = &self.data.assignments[start + i];
                let sc = a.status_color();
                let issue = format!("#{:<5}", a.issue_number);
                let repo = format!("{:<18}", trunc(&a.repo, 18));
                let st = a.status_label();
                let text = StyledText {
                    spans: vec![
                        StyledSpan::with_fg(issue, Color::rgb(150, 150, 240)),
                        StyledSpan::plain(repo),
                        StyledSpan::with_fg(st, sc),
                    ],
                };
                rows.push(TreeRow {
                    path: vec![g_idx as u16, i as u16],
                    indent: 1,
                    icon: None,
                    text,
                    badge: Some(Badge::plain(a.age_str())),
                    is_expanded: None, // leaf node
                    decoration: if a.status == "failed" {
                        Decoration::Error
                    } else {
                        Decoration::Normal
                    },
                    edit: None,
                });
            }
        }

        rows
    }

    /// Return the assignment currently selected in the Board tree, if any.
    ///
    /// Only leaf paths (length 2) map to assignments; group-header paths
    /// (length 1) return `None`.
    fn board_selected_assignment(&self) -> Option<&Assignment> {
        let path = self.board_tree.selected_path()?;
        if path.len() < 2 {
            return None; // group header, not a leaf
        }
        let group = path[0] as usize;
        let item = path[1] as usize;

        let n_running = self
            .data
            .assignments
            .iter()
            .filter(|a| a.status == "running")
            .count();
        let n_failed = self
            .data
            .assignments
            .iter()
            .filter(|a| a.status == "failed")
            .count();

        let idx = match group {
            0 => item,
            1 => n_running + item,
            2 => n_running + n_failed + item,
            _ => return None,
        };
        self.data.assignments.get(idx)
    }

    /// Toggle expand/collapse for the group that is currently selected (if
    /// the selection sits on a group header row).
    fn toggle_selected_group(&mut self) {
        if let Some(path) = self.board_tree.selected_path().cloned() {
            if path.len() == 1 {
                let g = path[0] as usize;
                if g < 3 {
                    self.board_groups_expanded[g] = !self.board_groups_expanded[g];
                    self.rebuild_board_tree_rows();
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

    /// Left sidebar listing selectable views.
    fn sidebar_list(&self) -> ListView {
        const VIEWS: &[(&str, SidebarView)] = &[
            ("Board", SidebarView::Board),
            ("Machines", SidebarView::Machines),
            ("Pipeline", SidebarView::Pipeline),
        ];

        let items: Vec<ListItem> = VIEWS
            .iter()
            .map(|(label, view)| {
                let is_active = *view == self.active_view;
                let text = StyledText {
                    spans: vec![
                        StyledSpan::with_fg(
                            if is_active { "▶ " } else { "  " },
                            Color::rgb(100, 160, 220),
                        ),
                        StyledSpan::with_fg(
                            *label,
                            if is_active {
                                Color::rgb(255, 255, 255)
                            } else {
                                Color::rgb(160, 160, 170)
                            },
                        ),
                    ],
                };
                ListItem {
                    text,
                    icon: None,
                    detail: None,
                    decoration: Decoration::Normal,
                }
            })
            .collect();

        ListView {
            id: WidgetId::new("sidebar"),
            title: Some(StyledText::plain(" VIEWS ")),
            items,
            selected_idx: self.active_view.index(),
            scroll_offset: 0,
            has_focus: false,
            bordered: false,
        }
    }

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

        match self.board_selected_assignment() {
            None => {
                items.push(kv_item("", " No assignment selected", None));
            }
            Some(a) => {
                // Section header
                let header_text = format!(" {} #{} ", a.repo, a.issue_number);
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

                // Issue title (truncated to fit panel width)
                items.push(kv_item("", &format!("  {}", trunc(&a.issue_title, 52)), None));
                items.push(kv_item("", "", None)); // blank separator

                // Key-value field rows
                items.push(kv_item("ID", trunc(&a.id, 12), None));
                items.push(kv_item("Machine", &a.machine, None));
                items.push(kv_item(
                    "Status",
                    a.status_label().trim(),
                    Some(a.status_color()),
                ));
                if let Some(m) = &a.model {
                    items.push(kv_item("Model", m, None));
                }
                if let Some(b) = &a.branch {
                    items.push(kv_item("Branch", trunc(b, 44), None));
                } else {
                    items.push(kv_item(
                        "Branch",
                        "(none yet)",
                        Some(Color::rgb(100, 100, 100)),
                    ));
                }
                if let Some(t) = &a.assignment_type {
                    items.push(kv_item("Type", t, None));
                }
                items.push(kv_item("Age", &a.age_str(), None));

                if let Some(code) = a.exit_code {
                    let (s, c) = if code == 0 {
                        (format!("{} (ok)", code), Some(Color::rgb(80, 210, 80)))
                    } else {
                        (format!("{} (err)", code), Some(Color::rgb(210, 70, 70)))
                    };
                    items.push(kv_item("Exit code", &s, c));
                }

                if let (Some(start), Some(end)) = (a.dispatched_at, a.finished_at) {
                    let dur = (end - start).max(0.0) as u64;
                    items.push(kv_item("Duration", &fmt_dur(dur), None));
                }
            }
        }

        let title = match self.board_selected_assignment() {
            Some(a) => format!(" DETAIL — {} #{} ", a.repo, a.issue_number),
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

    /// Activity tab: live feed of worker events parsed from the log file.
    fn activity_list(&self) -> ListView {
        let (title, items) = match self.board_selected_assignment() {
            None => (
                " ACTIVITY ".to_string(),
                vec![kv_item("", " No assignment selected", None)],
            ),
            Some(a) => {
                let log_items = load_activity_log(&a.id);
                (
                    format!(" ACTIVITY — {} #{} ", a.repo, a.issue_number),
                    log_items,
                )
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

    /// Clamp `pipeline_scroll` so that `pipeline_sel` is inside the visible window.
    fn fix_pipeline_scroll(&mut self, visible: usize) {
        if visible == 0 {
            return;
        }
        if self.pipeline_sel < self.pipeline_scroll {
            self.pipeline_scroll = self.pipeline_sel;
        } else if self.pipeline_sel >= self.pipeline_scroll + visible {
            self.pipeline_scroll = self.pipeline_sel + 1 - visible;
        }
    }

    /// Return the unique issues present in assignments, ordered by most-recent
    /// dispatched_at descending. Each entry is `(issue_number, repo, title)`.
    fn pipeline_issues(&self) -> Vec<(u64, String, String)> {
        use std::collections::HashMap;
        // Map issue_number → (repo, title, max_dispatched_at)
        let mut map: HashMap<u64, (String, String, f64)> = HashMap::new();
        for a in &self.data.assignments {
            let ts = a.dispatched_at.unwrap_or(0.0);
            let entry = map
                .entry(a.issue_number)
                .or_insert_with(|| (a.repo.clone(), a.issue_title.clone(), ts));
            if ts > entry.2 {
                entry.2 = ts;
            }
        }
        let mut result: Vec<(u64, String, String, f64)> = map
            .into_iter()
            .map(|(n, (r, t, ts))| (n, r, t, ts))
            .collect();
        result.sort_by(|a, b| {
            b.3.partial_cmp(&a.3).unwrap_or(std::cmp::Ordering::Equal)
        });
        result.into_iter().map(|(n, r, t, _)| (n, r, t)).collect()
    }

    /// Left panel for the Pipeline view: list of unique issues.
    fn pipeline_issue_list(&self) -> ListView {
        let issues = self.pipeline_issues();
        let n = issues.len();

        let items: Vec<ListItem> = issues
            .iter()
            .map(|(issue_num, repo, title)| {
                // Collect all assignments for this issue to show aggregate status.
                let issue_assignments: Vec<&Assignment> = self
                    .data
                    .assignments
                    .iter()
                    .filter(|a| a.issue_number == *issue_num)
                    .collect();
                let (sc, bullet) = if issue_assignments
                    .iter()
                    .any(|a| a.status == "running")
                {
                    (Color::rgb(80, 220, 80), "~ ")
                } else if issue_assignments.iter().any(|a| a.status == "failed") {
                    (Color::rgb(220, 70, 70), "✗ ")
                } else if issue_assignments.iter().all(|a| a.status == "done") {
                    (Color::rgb(120, 180, 120), "✓ ")
                } else {
                    (Color::rgb(140, 140, 160), "- ")
                };
                let text = StyledText {
                    spans: vec![
                        StyledSpan::with_fg(bullet, sc),
                        StyledSpan::with_fg(
                            format!("#{} ", issue_num),
                            Color::rgb(150, 150, 240),
                        ),
                        StyledSpan::plain(trunc(title, 22)),
                    ],
                };
                let detail = Some(StyledText {
                    spans: vec![StyledSpan::with_fg(
                        trunc(repo, 14),
                        Color::rgb(100, 130, 170),
                    )],
                });
                ListItem {
                    text,
                    icon: None,
                    detail,
                    decoration: Decoration::Normal,
                }
            })
            .collect();

        ListView {
            id: WidgetId::new("pipeline"),
            title: Some(StyledText::plain(format!(" PIPELINE ({}) ", n))),
            items,
            selected_idx: if n > 0 { self.pipeline_sel } else { 0 },
            scroll_offset: self.pipeline_scroll,
            has_focus: true,
            bordered: false,
        }
    }

    /// Right panel for the Pipeline view: per-stage breakdown for the
    /// selected issue.
    fn pipeline_detail_list(&self) -> ListView {
        let issues = self.pipeline_issues();

        let mut items: Vec<ListItem> = Vec::new();

        match issues.get(self.pipeline_sel) {
            None => {
                items.push(kv_item("", " No issue selected", None));
            }
            Some((issue_number, repo, title)) => {
                // Section header
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(
                            format!(" {} #{} ", repo, issue_number),
                            Color::rgb(210, 220, 255),
                        )],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                });
                items.push(kv_item(
                    "",
                    &format!("  {}", trunc(title, 52)),
                    None,
                ));
                items.push(kv_item("", "", None)); // blank spacer

                // Sub-header
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(
                            " STAGES ",
                            Color::rgb(130, 130, 150),
                        )],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                });

                // All assignments for this issue (assignments sorted most-recent first).
                let issue_assignments: Vec<&Assignment> = self
                    .data
                    .assignments
                    .iter()
                    .filter(|a| a.issue_number == *issue_number)
                    .collect();

                // Stage definitions: (display name, type filter fn)
                // We render them in pipeline order even if some are absent.
                let stage_filters: &[(&str, fn(&Assignment) -> bool)] = &[
                    ("Plan", |a| {
                        a.assignment_type.as_deref() == Some("plan")
                    }),
                    ("Work", |a| {
                        a.assignment_type.as_deref() == Some("work")
                            || a.assignment_type.is_none()
                    }),
                    ("Review", |a| {
                        a.assignment_type.as_deref() == Some("review")
                    }),
                    ("Smoke", |a| {
                        a.assignment_type.as_deref() == Some("smoke")
                    }),
                ];

                for (stage_name, filter) in stage_filters {
                    let assignment = issue_assignments.iter().find(|a| filter(a));
                    items.push(pipeline_stage_item(stage_name, assignment.copied()));
                }

                // PR/Merge stage from merge_queue
                let mq_entry = self
                    .data
                    .merge_queue
                    .iter()
                    .find(|e| e.issue_number == Some(*issue_number));
                items.push(pipeline_merge_item(mq_entry));
            }
        }

        let title = match issues.get(self.pipeline_sel) {
            Some((n, r, _)) => format!(" PIPELINE — {} #{} ", r, n),
            None => " PIPELINE ".to_string(),
        };

        ListView {
            id: WidgetId::new("pipeline-detail"),
            title: Some(StyledText::plain(&title)),
            items,
            selected_idx: 0,
            scroll_offset: self.pipeline_detail_scroll,
            has_focus: false,
            bordered: false,
        }
    }

    // ── Mouse dispatch ────────────────────────────────────────────────────

    /// Dispatch one mouse event. Called from `handle()` before the keyboard
    /// match so we can still pass `&UiEvent` to `board_tree.handle()`.
    /// Returns `true` if a redraw is needed.
    fn handle_mouse(&mut self, event: &UiEvent, backend: &mut dyn Backend) -> bool {
        match event {
            UiEvent::MouseDown {
                position,
                button: MouseButton::Left,
                ..
            } => {
                let pos = *position;
                let (sidebar_b, first_b, second_b) = compute_panel_layout(backend);
                let lh = backend.line_height();
                if sidebar_b.contains(pos) {
                    self.mouse_sidebar_click(pos, sidebar_b)
                } else if first_b.contains(pos) {
                    self.mouse_first_click(event, pos, first_b, backend)
                } else if second_b.contains(pos) {
                    self.mouse_second_click(pos, second_b, lh)
                } else {
                    false
                }
            }

            UiEvent::Scroll { position, delta, .. } => {
                let pos = *position;
                let d = *delta;
                let (_, first_b, second_b) = compute_panel_layout(backend);
                if first_b.contains(pos) {
                    self.mouse_first_scroll(event, d, first_b, backend)
                } else if second_b.contains(pos) {
                    self.mouse_second_scroll(d, backend)
                } else {
                    false
                }
            }

            _ => false,
        }
    }

    /// Click in the left sidebar — switch to the clicked view.
    fn mouse_sidebar_click(&mut self, pos: Point, sidebar_b: Rect) -> bool {
        // Row 0 of the panel is the title; views start at row 1.
        let row = (pos.y - sidebar_b.y).max(0.0) as usize;
        let view = match row.saturating_sub(1) {
            0 => Some(SidebarView::Board),
            1 => Some(SidebarView::Machines),
            2 => Some(SidebarView::Pipeline),
            _ => None,
        };
        if let Some(v) = view {
            if v != self.active_view {
                self.active_view = v;
                return true;
            }
        }
        false
    }

    /// Click in the left content panel (board tree / machines list / pipeline
    /// list, depending on the active view).
    fn mouse_first_click(
        &mut self,
        event: &UiEvent,
        pos: Point,
        first_b: Rect,
        backend: &mut dyn Backend,
    ) -> bool {
        match self.active_view {
            SidebarView::Board => {
                // Delegate fully to the TreeController — it handles row
                // hit-testing, selection, and scroll-thumb dragging.
                let result = self.board_tree.handle(event, backend, first_b);
                if let TreeControllerEvent::RowSelected { ref path } = result {
                    // Reset detail-panel scroll when the user picks a new leaf.
                    if path.len() >= 2 {
                        self.detail_scroll = 0;
                        self.activity_scroll = None; // back to auto-scroll
                    }
                }
                result != TreeControllerEvent::Ignored
            }
            SidebarView::Machines => {
                // Row 0 = title strip; item i starts at row 1+i-scroll.
                let row = (pos.y - first_b.y).max(0.0) as usize;
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
                let row = (pos.y - first_b.y).max(0.0) as usize;
                if row >= 1 {
                    let item_idx = (row - 1) + self.pipeline_scroll;
                    let p = self.pipeline_issues().len();
                    if item_idx < p && item_idx != self.pipeline_sel {
                        self.pipeline_sel = item_idx;
                        self.pipeline_detail_scroll = 0;
                        return true;
                    }
                }
                false
            }
        }
    }

    /// Click in the right content panel — in Board view this handles the
    /// tab bar (first row of the panel).
    fn mouse_second_click(&mut self, pos: Point, second_b: Rect, lh: f32) -> bool {
        if self.active_view != SidebarView::Board {
            return false;
        }
        // The tab bar occupies the first `lh` pixels/cells of the panel.
        if pos.y - second_b.y < lh {
            // " Summary " is 9 chars; " Activity " is 10 chars (compact tabs,
            // no separator). Anything in the first 9 columns → Summary tab.
            let x_off = pos.x - second_b.x;
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

    /// Scroll wheel in the left content panel.
    fn mouse_first_scroll(
        &mut self,
        event: &UiEvent,
        delta: ScrollDelta,
        first_b: Rect,
        backend: &mut dyn Backend,
    ) -> bool {
        match self.active_view {
            SidebarView::Board => {
                // Delegate to the TreeController's built-in scroll handler.
                self.board_tree.handle(event, backend, first_b);
                true
            }
            SidebarView::Machines => {
                let visible = content_visible_rows(backend);
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
                let visible = content_visible_rows(backend);
                let p = self.pipeline_issues().len();
                if delta.y > 0.0 {
                    self.pipeline_scroll = self.pipeline_scroll.saturating_sub(1);
                } else if delta.y < 0.0 {
                    let max = p.saturating_sub(visible);
                    self.pipeline_scroll = (self.pipeline_scroll + 1).min(max);
                }
                true
            }
        }
    }

    /// Scroll wheel in the right content panel (detail / activity / machine
    /// detail / pipeline detail).
    fn mouse_second_scroll(&mut self, delta: ScrollDelta, backend: &dyn Backend) -> bool {
        let visible = content_visible_rows(backend);
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
                        // Build the activity list to find its item count; also
                        // resolves the current auto-scroll offset if needed.
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
                let items = self.pipeline_detail_list().items.len();
                let max = items.saturating_sub(visible.saturating_sub(1));
                if delta.y > 0.0 {
                    self.pipeline_detail_scroll = self.pipeline_detail_scroll.saturating_sub(1);
                } else if delta.y < 0.0 {
                    self.pipeline_detail_scroll = (self.pipeline_detail_scroll + 1).min(max);
                }
                true
            }
        }
    }

    fn status_bar(&self) -> StatusBar {
        let since = self.refreshed_at.elapsed().as_secs();
        let view_label = self.active_view.label();
        StatusBar {
            id: WidgetId::new("statusbar"),
            left_segments: vec![
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
                    text: format!(" ↻ {}s ", since),
                    fg: Color::rgb(140, 140, 140),
                    bg: Color::rgb(30, 30, 40),
                    bold: false,
                    action_id: None,
                },
            ],
            right_segments: vec![StatusBarSegment {
                text: " 1=Board  2=Machines  3=Pipeline  Tab=switch  j/k=nav  h/l=tabs  Enter/Space=expand  r=refresh  q=quit ".to_string(),
                fg: Color::rgb(140, 140, 140),
                bg: Color::rgb(30, 30, 40),
                bold: false,
                action_id: None,
            }],
        }
    }
}

// ─── Panel layout helper ──────────────────────────────────────────────────────

/// Recompute the panel bounds that `render()` produces, without drawing.
///
/// Returns `(sidebar_bounds, content_first_bounds, content_second_bounds)`.
/// Used in `handle()` to hit-test mouse events against the same layout that
/// the last frame drew, without requiring interior mutability.
///
/// The TUI backend always uses a divider thickness of 1 cell, so we pass
/// `SplitMeasure::new(1.0)` here to match the rendered layout exactly.
fn compute_panel_layout(backend: &dyn Backend) -> (Rect, Rect, Rect) {
    let vp = backend.viewport();
    let lh = backend.line_height();
    let main_rect = Rect::new(0.0, 0.0, vp.width, vp.height - lh);

    let outer = Split {
        id: WidgetId::new("sidebar-outer"),
        direction: SplitDirection::Horizontal,
        ratio: 0.18,
        first_min: 0.0,
        second_min: 0.0,
    }
    .layout(main_rect, SplitMeasure::new(1.0));

    let inner = Split {
        id: WidgetId::new("content-split"),
        direction: SplitDirection::Horizontal,
        ratio: 0.40,
        first_min: 0.0,
        second_min: 0.0,
    }
    .layout(outer.second_bounds, SplitMeasure::new(1.0));

    (outer.first_bounds, inner.first_bounds, inner.second_bounds)
}

// ─── AppLogic implementation ──────────────────────────────────────────────────

impl AppLogic for CoordApp {
    type AreaId = ();

    fn render(&self, backend: &mut dyn Backend, _area: ()) {
        let vp = backend.viewport();
        let lh = backend.line_height();

        // Reserve one line for the status bar at the bottom.
        let main_rect = Rect::new(0.0, 0.0, vp.width, vp.height - lh);

        // ── Outer split: 18% sidebar | 82% content ───────────────────
        let sidebar_split = Split {
            id: WidgetId::new("sidebar-outer"),
            direction: SplitDirection::Horizontal,
            ratio: 0.18,
            first_min: 0.0,
            second_min: 0.0,
        };
        let outer = backend.draw_split(main_rect, &sidebar_split);

        // Draw the sidebar navigation list
        backend.draw_list(outer.first_bounds, &self.sidebar_list());

        // ── Content split: 40% list | 60% detail ─────────────────────
        let content_split = Split {
            id: WidgetId::new("content-split"),
            direction: SplitDirection::Horizontal,
            ratio: 0.40,
            first_min: 0.0,
            second_min: 0.0,
        };
        let inner = backend.draw_split(outer.second_bounds, &content_split);

        // Draw the active view's panels
        match self.active_view {
            SidebarView::Board => {
                // TreeController::render reads self.board_tree (immutable) and
                // passes it to backend.draw_tree — no mutation needed here.
                self.board_tree.render(backend, inner.first_bounds);

                // Detail panel: tab bar (1 line) + tab content below.
                let d = inner.second_bounds;
                let tab_h = lh;
                let tab_rect = Rect::new(d.x, d.y, d.width, tab_h);
                let content_rect =
                    Rect::new(d.x, d.y + tab_h, d.width, (d.height - tab_h).max(0.0));
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
                backend.draw_list(inner.first_bounds, &self.machines_list(true));
                backend.draw_list(inner.second_bounds, &self.machine_detail_list());
            }
            SidebarView::Pipeline => {
                backend.draw_list(inner.first_bounds, &self.pipeline_issue_list());
                backend.draw_list(inner.second_bounds, &self.pipeline_detail_list());
            }
        }

        // Status bar
        let sb_rect = Rect::new(0.0, vp.height - lh, vp.width, lh);
        backend.draw_status_bar(sb_rect, &self.status_bar(), None, None);
    }

    fn tick(&mut self, _backend: &mut dyn Backend) -> Reaction {
        if self.refreshed_at.elapsed() >= REFRESH_EVERY {
            self.refresh();
            Reaction::Redraw
        } else {
            Reaction::Continue
        }
    }

    fn handle(&mut self, event: UiEvent, backend: &mut dyn Backend) -> Reaction {
        let mut needs_redraw = false;

        // ── Mouse / scroll dispatch (before consuming the event) ─────────────
        needs_redraw |= self.handle_mouse(&event, backend);

        // ── Keyboard and window events ────────────────────────────────────────
        match event {
            UiEvent::KeyPressed { key, .. } => {
                match key {
                    Key::Char('q') | Key::Named(NamedKey::Escape) => return Reaction::Exit,

                    // ── Switch sidebar views ─────────────────────────────
                    Key::Named(NamedKey::Tab) => {
                        self.active_view = self.active_view.next();
                        needs_redraw = true;
                    }
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
                        needs_redraw = true;
                    }

                    // ── Down / j ─────────────────────────────────────────
                    Key::Char('j') | Key::Named(NamedKey::Down) => {
                        match self.active_view {
                            SidebarView::Board => {
                                let vr = board_visible_rows(backend);
                                let prev = self.board_tree.selected_path().cloned();
                                self.board_tree.move_selection_by(1, vr);
                                if self.board_tree.selected_path() != prev.as_ref() {
                                    self.detail_scroll = 0;
                                    self.activity_scroll = None;
                                }
                            }
                            SidebarView::Machines => {
                                let m = self.data.machines.len();
                                if m > 0 && self.machine_sel + 1 < m {
                                    self.machine_sel += 1;
                                    self.machine_detail_scroll = 0;
                                }
                                self.fix_machine_scroll(content_visible_rows(backend));
                            }
                            SidebarView::Pipeline => {
                                let p = self.pipeline_issues().len();
                                if p > 0 && self.pipeline_sel + 1 < p {
                                    self.pipeline_sel += 1;
                                    self.pipeline_detail_scroll = 0;
                                }
                                self.fix_pipeline_scroll(content_visible_rows(backend));
                            }
                        }
                        needs_redraw = true;
                    }

                    // ── Up / k ───────────────────────────────────────────
                    Key::Char('k') | Key::Named(NamedKey::Up) => {
                        match self.active_view {
                            SidebarView::Board => {
                                let vr = board_visible_rows(backend);
                                let prev = self.board_tree.selected_path().cloned();
                                self.board_tree.move_selection_by(-1, vr);
                                if self.board_tree.selected_path() != prev.as_ref() {
                                    self.detail_scroll = 0;
                                    self.activity_scroll = None;
                                }
                            }
                            SidebarView::Machines => {
                                if self.machine_sel > 0 {
                                    self.machine_sel -= 1;
                                    self.machine_detail_scroll = 0;
                                }
                                self.fix_machine_scroll(content_visible_rows(backend));
                            }
                            SidebarView::Pipeline => {
                                if self.pipeline_sel > 0 {
                                    self.pipeline_sel -= 1;
                                    self.pipeline_detail_scroll = 0;
                                }
                                self.fix_pipeline_scroll(content_visible_rows(backend));
                            }
                        }
                        needs_redraw = true;
                    }

                    // ── Home ─────────────────────────────────────────────
                    Key::Named(NamedKey::Home) => {
                        match self.active_view {
                            SidebarView::Board => {
                                let vr = board_visible_rows(backend);
                                let prev = self.board_tree.selected_path().cloned();
                                self.board_tree.jump_to_edge(true, vr);
                                if self.board_tree.selected_path() != prev.as_ref() {
                                    self.detail_scroll = 0;
                                    self.activity_scroll = None;
                                }
                            }
                            SidebarView::Machines => {
                                self.machine_sel = 0;
                                self.machine_detail_scroll = 0;
                                self.fix_machine_scroll(content_visible_rows(backend));
                            }
                            SidebarView::Pipeline => {
                                self.pipeline_sel = 0;
                                self.pipeline_detail_scroll = 0;
                                self.fix_pipeline_scroll(content_visible_rows(backend));
                            }
                        }
                        needs_redraw = true;
                    }

                    // ── End ──────────────────────────────────────────────
                    Key::Named(NamedKey::End) => {
                        match self.active_view {
                            SidebarView::Board => {
                                let vr = board_visible_rows(backend);
                                let prev = self.board_tree.selected_path().cloned();
                                self.board_tree.jump_to_edge(false, vr);
                                if self.board_tree.selected_path() != prev.as_ref() {
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
                                self.fix_machine_scroll(content_visible_rows(backend));
                            }
                            SidebarView::Pipeline => {
                                let p = self.pipeline_issues().len();
                                if p > 0 {
                                    self.pipeline_sel = p - 1;
                                    self.pipeline_detail_scroll = 0;
                                }
                                self.fix_pipeline_scroll(content_visible_rows(backend));
                            }
                        }
                        needs_redraw = true;
                    }

                    // ── PageDown (Board only) ─────────────────────────────
                    Key::Named(NamedKey::PageDown)
                        if self.active_view == SidebarView::Board =>
                    {
                        let vr = board_visible_rows(backend);
                        let jump = (vr.max(1) - 1).max(1) as isize;
                        let prev = self.board_tree.selected_path().cloned();
                        self.board_tree.move_selection_by(jump, vr);
                        if self.board_tree.selected_path() != prev.as_ref() {
                            self.detail_scroll = 0;
                            self.activity_scroll = None;
                        }
                        needs_redraw = true;
                    }

                    // ── PageUp (Board only) ───────────────────────────────
                    Key::Named(NamedKey::PageUp)
                        if self.active_view == SidebarView::Board =>
                    {
                        let vr = board_visible_rows(backend);
                        let jump = (vr.max(1) - 1).max(1) as isize;
                        let prev = self.board_tree.selected_path().cloned();
                        self.board_tree.move_selection_by(-jump, vr);
                        if self.board_tree.selected_path() != prev.as_ref() {
                            self.detail_scroll = 0;
                            self.activity_scroll = None;
                        }
                        needs_redraw = true;
                    }

                    // ── Enter / Space — expand/collapse group (Board only) ─
                    Key::Named(NamedKey::Enter) | Key::Char(' ')
                        if self.active_view == SidebarView::Board =>
                    {
                        self.toggle_selected_group();
                        needs_redraw = true;
                    }

                    // ── Left / h — switch to Summary tab (Board only) ─────
                    Key::Named(NamedKey::Left) | Key::Char('h')
                        if self.active_view == SidebarView::Board =>
                    {
                        if self.detail_tab != DetailTab::Summary {
                            self.detail_tab = DetailTab::Summary;
                            needs_redraw = true;
                        }
                    }

                    // ── Right / l — switch to Activity tab (Board only) ───
                    Key::Named(NamedKey::Right) | Key::Char('l')
                        if self.active_view == SidebarView::Board =>
                    {
                        if self.detail_tab != DetailTab::Activity {
                            self.detail_tab = DetailTab::Activity;
                            needs_redraw = true;
                        }
                    }

                    Key::Char('r') => {
                        self.refresh();
                        needs_redraw = true;
                    }

                    _ => {}
                }
            }

            UiEvent::WindowResized { .. } => needs_redraw = true,

            _ => {}
        }

        if needs_redraw {
            Reaction::Redraw
        } else {
            Reaction::Continue
        }
    }
}

/// Estimate the number of visible rows in the Machines list panel.
///
/// The panel occupies the full terminal height minus the status bar row and
/// the list title row.
fn content_visible_rows(backend: &dyn Backend) -> usize {
    let vp = backend.viewport();
    let lh = backend.line_height();
    if lh <= 0.0 {
        return 10;
    }
    let main_h = vp.height - lh; // minus status bar
    let content_h = (main_h - lh).max(0.0); // minus list title row
    (content_h / lh) as usize
}

/// Estimate the number of visible rows in the Board tree panel.
///
/// Used to provide a viewport-rows hint to [`TreeController`] navigation
/// primitives. The Board tree has no separate title row (unlike `ListView`),
/// so we deduct only the status bar row.
fn board_visible_rows(backend: &dyn Backend) -> usize {
    let vp = backend.viewport();
    let lh = backend.line_height();
    if lh <= 0.0 {
        return 10;
    }
    let main_h = vp.height - lh; // minus status bar
    (main_h / lh) as usize
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

    // ── Board tree helpers ────────────────────────────────────────────────────

    fn make_app_default() -> CoordApp {
        CoordApp {
            data: BoardData::default(),
            active_view: SidebarView::default(),
            board_tree: TreeController::new("board"),
            board_groups_expanded: [true, true, false],
            machine_sel: 0,
            machine_scroll: 0,
            refreshed_at: Instant::now(),
            detail_tab: DetailTab::default(),
            pipeline_sel: 0,
            pipeline_scroll: 0,
            detail_scroll: 0,
            activity_scroll: None,
            machine_detail_scroll: 0,
            pipeline_detail_scroll: 0,
        }
    }

    fn make_app_with_assignments(assignments: Vec<Assignment>) -> CoordApp {
        let mut app = CoordApp {
            data: BoardData {
                assignments,
                ..BoardData::default()
            },
            active_view: SidebarView::default(),
            board_tree: TreeController::new("board"),
            board_groups_expanded: [true, true, false],
            machine_sel: 0,
            machine_scroll: 0,
            refreshed_at: Instant::now(),
            detail_tab: DetailTab::default(),
            pipeline_sel: 0,
            pipeline_scroll: 0,
            detail_scroll: 0,
            activity_scroll: None,
            machine_detail_scroll: 0,
            pipeline_detail_scroll: 0,
        };
        app.rebuild_board_tree_rows();
        app
    }

    // ── build_board_rows / board_selected_assignment ──────────────────────────

    #[test]
    fn board_rows_empty_data_produces_three_group_headers() {
        let app = make_app_default();
        let rows = app.build_board_rows();
        // Three group headers (Running, Failed, Done) — all expanded=true/true/false.
        // Running(0) + Failed(0) both expanded → 0 leaves; Done collapsed → 0 leaves.
        assert_eq!(rows.len(), 3);
        assert_eq!(rows[0].path, vec![0]);
        assert_eq!(rows[1].path, vec![1]);
        assert_eq!(rows[2].path, vec![2]);
    }

    #[test]
    fn board_rows_expanded_group_shows_leaves() {
        let assignments = vec![
            make_assignment("running"),
            make_assignment("running"),
            make_assignment("failed"),
        ];
        let app = make_app_with_assignments(assignments);
        let rows = app.build_board_rows();
        // Running(2 expanded) + 2 leaves + Failed(1 expanded) + 1 leaf + Done(0 collapsed)
        assert_eq!(rows.len(), 3 + 2 + 1); // 3 headers + 3 leaves
        // First header: Running group
        assert_eq!(rows[0].path, vec![0]);
        assert_eq!(rows[0].is_expanded, Some(true));
        // Leaves for running
        assert_eq!(rows[1].path, vec![0, 0]);
        assert_eq!(rows[2].path, vec![0, 1]);
        // Failed header
        assert_eq!(rows[3].path, vec![1]);
        assert_eq!(rows[3].is_expanded, Some(true));
        // Leaf for failed
        assert_eq!(rows[4].path, vec![1, 0]);
        // Done header — collapsed by default
        assert_eq!(rows[5].path, vec![2]);
        assert_eq!(rows[5].is_expanded, Some(false));
    }

    #[test]
    fn board_rows_collapsed_group_hides_leaves() {
        let assignments = vec![make_assignment("done"), make_assignment("done")];
        let app = make_app_with_assignments(assignments);
        let rows = app.build_board_rows();
        // Running(0) + Failed(0) expanded with 0 leaves; Done(2) collapsed → 0 leaves
        assert_eq!(rows.len(), 3); // only the 3 headers
    }

    #[test]
    fn board_selected_assignment_on_leaf_returns_correct_assignment() {
        let assignments = vec![
            make_assignment("running"), // idx 0 → path [0, 0]
            make_assignment("failed"),  // idx 1 → path [1, 0]
            make_assignment("done"),    // idx 2 → path [2, 0]  (collapsed, won't be selectable)
        ];
        let mut app = make_app_with_assignments(assignments.clone());

        // Select running leaf [0, 0]
        app.board_tree.set_selected_path(Some(vec![0, 0]));
        let sel = app.board_selected_assignment().unwrap();
        assert_eq!(sel.status, "running");

        // Select failed leaf [1, 0]
        app.board_tree.set_selected_path(Some(vec![1, 0]));
        let sel = app.board_selected_assignment().unwrap();
        assert_eq!(sel.status, "failed");
    }

    #[test]
    fn board_selected_assignment_on_group_header_returns_none() {
        let assignments = vec![make_assignment("running")];
        let mut app = make_app_with_assignments(assignments);

        // Select the Running group header [0]
        app.board_tree.set_selected_path(Some(vec![0]));
        assert!(app.board_selected_assignment().is_none());
    }

    #[test]
    fn board_selected_assignment_no_selection_returns_none() {
        let app = make_app_default();
        assert!(app.board_selected_assignment().is_none());
    }

    #[test]
    fn toggle_selected_group_expands_collapsed_done_group() {
        let assignments = vec![make_assignment("done")];
        let mut app = make_app_with_assignments(assignments);
        // Done is group index 2, collapsed by default
        app.board_tree.set_selected_path(Some(vec![2]));
        assert!(!app.board_groups_expanded[2]);

        app.toggle_selected_group();
        assert!(app.board_groups_expanded[2]);

        let rows = app.build_board_rows();
        // Now Done expanded → 1 leaf visible
        let done_leaf = rows.iter().any(|r| r.path == vec![2, 0]);
        assert!(done_leaf, "done leaf should be visible after expand");
    }

    #[test]
    fn toggle_selected_group_collapses_expanded_running_group() {
        let assignments = vec![make_assignment("running")];
        let mut app = make_app_with_assignments(assignments);
        app.board_tree.set_selected_path(Some(vec![0]));
        assert!(app.board_groups_expanded[0]); // expanded by default

        app.toggle_selected_group();
        assert!(!app.board_groups_expanded[0]);

        let rows = app.build_board_rows();
        // After collapse, no running leaf
        let has_leaf = rows.iter().any(|r| r.path.len() == 2 && r.path[0] == 0);
        assert!(!has_leaf, "running leaves should be hidden after collapse");
    }

    #[test]
    fn rebuild_clears_stale_selection_on_collapse() {
        let assignments = vec![make_assignment("running")];
        let mut app = make_app_with_assignments(assignments);
        // Select the running leaf
        app.board_tree.set_selected_path(Some(vec![0, 0]));
        assert!(app.board_selected_assignment().is_some());

        // Collapse running group — leaf path [0, 0] no longer exists
        app.board_groups_expanded[0] = false;
        app.rebuild_board_tree_rows();

        // Selection should have been cleared
        assert!(app.board_tree.selected_path().is_none());
    }

    // ── fix_machine_scroll ────────────────────────────────────────────────────

    fn make_app_machine(machine_sel: usize, machine_scroll: usize) -> CoordApp {
        CoordApp {
            data: BoardData::default(),
            active_view: SidebarView::Machines,
            board_tree: TreeController::new("board"),
            board_groups_expanded: [true, true, false],
            machine_sel,
            machine_scroll,
            refreshed_at: Instant::now(),
            detail_tab: DetailTab::default(),
            pipeline_sel: 0,
            pipeline_scroll: 0,
            detail_scroll: 0,
            activity_scroll: None,
            machine_detail_scroll: 0,
            pipeline_detail_scroll: 0,
        }
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
    fn sidebar_view_next_cycles() {
        assert_eq!(SidebarView::Board.next(), SidebarView::Machines);
        assert_eq!(SidebarView::Machines.next(), SidebarView::Pipeline);
        assert_eq!(SidebarView::Pipeline.next(), SidebarView::Board);
    }

    #[test]
    fn sidebar_view_index() {
        assert_eq!(SidebarView::Board.index(), 0);
        assert_eq!(SidebarView::Machines.index(), 1);
        assert_eq!(SidebarView::Pipeline.index(), 2);
    }

    #[test]
    fn sidebar_view_label() {
        assert_eq!(SidebarView::Board.label(), "Board");
        assert_eq!(SidebarView::Machines.label(), "Machines");
        assert_eq!(SidebarView::Pipeline.label(), "Pipeline");
    }

    #[test]
    fn sidebar_view_default_is_board() {
        assert_eq!(SidebarView::default(), SidebarView::Board);
    }

    // ── Pipeline ─────────────────────────────────────────────────────────────

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
    fn pipeline_issues_deduplicates_by_issue_number() {
        let assignments = vec![
            make_assignment_typed("running", 10, "repo-a", Some("work")),
            make_assignment_typed("done", 10, "repo-a", Some("plan")),
            make_assignment_typed("done", 20, "repo-b", Some("work")),
        ];
        let app = make_app_with_assignments(assignments);
        let issues = app.pipeline_issues();
        assert_eq!(issues.len(), 2);
        let nums: Vec<u64> = issues.iter().map(|(n, _, _)| *n).collect();
        assert!(nums.contains(&10));
        assert!(nums.contains(&20));
    }

    #[test]
    fn pipeline_issues_orders_by_most_recent_dispatched_at() {
        let mut a_old = make_assignment_typed("done", 5, "repo", Some("work"));
        a_old.dispatched_at = Some(1_000.0);
        let mut a_new = make_assignment_typed("done", 7, "repo", Some("work"));
        a_new.dispatched_at = Some(9_000.0);
        let app = make_app_with_assignments(vec![a_old, a_new]);
        let issues = app.pipeline_issues();
        // Issue 7 (newer) should come first.
        assert_eq!(issues[0].0, 7);
        assert_eq!(issues[1].0, 5);
    }

    #[test]
    fn pipeline_issues_empty_when_no_assignments() {
        let app = make_app_default();
        assert!(app.pipeline_issues().is_empty());
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
    fn mouse_sidebar_click_switches_view() {
        let mut app = make_app_default();
        assert_eq!(app.active_view, SidebarView::Board);

        // Simulate a click at row 2 in the sidebar (row 0 = title, 1 = Board,
        // 2 = Machines, 3 = Pipeline).
        let sidebar_b = Rect::new(0.0, 0.0, 14.0, 40.0);
        let changed = app.mouse_sidebar_click(Point::new(2.0, 2.0), sidebar_b);
        assert!(changed);
        assert_eq!(app.active_view, SidebarView::Machines);
    }

    #[test]
    fn mouse_sidebar_click_same_view_no_redraw() {
        let mut app = make_app_default();
        let sidebar_b = Rect::new(0.0, 0.0, 14.0, 40.0);
        // Row 1 = Board, which is already active.
        let changed = app.mouse_sidebar_click(Point::new(2.0, 1.0), sidebar_b);
        assert!(!changed);
        assert_eq!(app.active_view, SidebarView::Board);
    }

    #[test]
    fn mouse_sidebar_click_pipeline_view() {
        let mut app = make_app_default();
        let sidebar_b = Rect::new(0.0, 0.0, 14.0, 40.0);
        // Row 3 = Pipeline.
        let changed = app.mouse_sidebar_click(Point::new(2.0, 3.0), sidebar_b);
        assert!(changed);
        assert_eq!(app.active_view, SidebarView::Pipeline);
    }

    #[test]
    fn mouse_second_click_tab_summary() {
        let mut app = make_app_default();
        app.detail_tab = DetailTab::Activity;
        let second_b = Rect::new(50.0, 0.0, 40.0, 40.0);
        // Click at x=51 → offset 1 < 9 → Summary tab.
        let changed = app.mouse_second_click(Point::new(51.0, 0.0), second_b, 1.0);
        assert!(changed);
        assert_eq!(app.detail_tab, DetailTab::Summary);
    }

    #[test]
    fn mouse_second_click_tab_activity() {
        let mut app = make_app_default();
        assert_eq!(app.detail_tab, DetailTab::Summary);
        let second_b = Rect::new(50.0, 0.0, 40.0, 40.0);
        // Click at x=60 → offset 10 ≥ 9 → Activity tab.
        let changed = app.mouse_second_click(Point::new(60.0, 0.0), second_b, 1.0);
        assert!(changed);
        assert_eq!(app.detail_tab, DetailTab::Activity);
    }

    #[test]
    fn mouse_second_click_below_tab_row_is_ignored() {
        let mut app = make_app_default();
        let second_b = Rect::new(50.0, 0.0, 40.0, 40.0);
        // Click at row y=2, well below the tab bar at y=0.
        let changed = app.mouse_second_click(Point::new(55.0, 2.0), second_b, 1.0);
        assert!(!changed);
    }

    #[test]
    fn mouse_second_click_non_board_view_is_ignored() {
        let mut app = make_app_default();
        app.active_view = SidebarView::Machines;
        let second_b = Rect::new(50.0, 0.0, 40.0, 40.0);
        let changed = app.mouse_second_click(Point::new(55.0, 0.0), second_b, 1.0);
        assert!(!changed);
    }

    // ── compute_panel_layout ──────────────────────────────────────────────────

    #[test]
    fn compute_panel_layout_sidebar_starts_at_origin() {
        use quadraui::Viewport;
        // Use a mock backend substitute: we can exercise layout math directly.
        let vp = Viewport::new(120.0, 40.0, 1.0);
        let lh = 1.0_f32;
        let main_rect = Rect::new(0.0, 0.0, vp.width, vp.height - lh);

        let outer = Split {
            id: WidgetId::new("sidebar-outer"),
            direction: SplitDirection::Horizontal,
            ratio: 0.18,
            first_min: 0.0,
            second_min: 0.0,
        }
        .layout(main_rect, SplitMeasure::new(1.0));

        // Sidebar x should be 0, content should start after sidebar + divider.
        assert_eq!(outer.first_bounds.x, 0.0);
        assert!(outer.second_bounds.x > outer.first_bounds.x);
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

    #[test]
    fn pipeline_detail_scroll_preserved_after_refresh() {
        let mut app = make_app_default();
        app.pipeline_detail_scroll = 2;
        app.refresh();
        assert_eq!(app.pipeline_detail_scroll, 2);
    }
}
