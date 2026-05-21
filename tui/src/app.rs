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
    AppLogic, Backend, Badge, Color, Decoration, Key, ListItem, ListView, NamedKey, Reaction, Rect,
    Split, SplitDirection, StatusBar, StatusBarSegment, StyledSpan, StyledText, TreeController,
    TreeRow, UiEvent, WidgetId,
};

// ─── Auto-refresh interval ────────────────────────────────────────────────────

/// Reload board data every 5 seconds.
const REFRESH_EVERY: Duration = Duration::from_secs(5);

// ─── Sidebar views ────────────────────────────────────────────────────────────

/// The selectable top-level views shown in the left sidebar.
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
enum SidebarView {
    #[default]
    Board,
    Machines,
}

impl SidebarView {
    fn label(self) -> &'static str {
        match self {
            SidebarView::Board => "Board",
            SidebarView::Machines => "Machines",
        }
    }

    fn index(self) -> usize {
        match self {
            SidebarView::Board => 0,
            SidebarView::Machines => 1,
        }
    }

    /// Cycle to the next view (wraps around).
    fn next(self) -> Self {
        match self {
            SidebarView::Board => SidebarView::Machines,
            SidebarView::Machines => SidebarView::Board,
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

#[derive(Default)]
struct BoardData {
    local_machine: String,
    assignments: Vec<Assignment>,
    machines: Vec<Machine>,
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

    BoardData {
        local_machine,
        assignments,
        machines,
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
            scroll_offset: 0,
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
            scroll_offset: 0,
            has_focus: false,
            bordered: false,
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
                text: " 1=Board  2=Machines  Tab=switch  j/k=nav  Enter/Space=expand  r=refresh  q=quit ".to_string(),
                fg: Color::rgb(140, 140, 140),
                bg: Color::rgb(30, 30, 40),
                bold: false,
                action_id: None,
            }],
        }
    }
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
                backend.draw_list(inner.second_bounds, &self.detail_list());
            }
            SidebarView::Machines => {
                backend.draw_list(inner.first_bounds, &self.machines_list(true));
                backend.draw_list(inner.second_bounds, &self.machine_detail_list());
            }
        }

        // Status bar
        let sb_rect = Rect::new(0.0, vp.height - lh, vp.width, lh);
        backend.draw_status_bar(sb_rect, &self.status_bar(), None, None);
    }

    fn handle(&mut self, event: UiEvent, backend: &mut dyn Backend) -> Reaction {
        // Auto-refresh: reload board data if the interval has elapsed.
        // Checked on every event so we don't need a dedicated tick callback.
        let mut needs_redraw = if self.refreshed_at.elapsed() >= REFRESH_EVERY {
            self.refresh();
            true
        } else {
            false
        };

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

                    // ── Down / j ─────────────────────────────────────────
                    Key::Char('j') | Key::Named(NamedKey::Down) => {
                        match self.active_view {
                            SidebarView::Board => {
                                let vr = board_visible_rows(backend);
                                self.board_tree.move_selection_by(1, vr);
                            }
                            SidebarView::Machines => {
                                let m = self.data.machines.len();
                                if m > 0 && self.machine_sel + 1 < m {
                                    self.machine_sel += 1;
                                }
                                self.fix_machine_scroll(content_visible_rows(backend));
                            }
                        }
                        needs_redraw = true;
                    }

                    // ── Up / k ───────────────────────────────────────────
                    Key::Char('k') | Key::Named(NamedKey::Up) => {
                        match self.active_view {
                            SidebarView::Board => {
                                let vr = board_visible_rows(backend);
                                self.board_tree.move_selection_by(-1, vr);
                            }
                            SidebarView::Machines => {
                                if self.machine_sel > 0 {
                                    self.machine_sel -= 1;
                                }
                                self.fix_machine_scroll(content_visible_rows(backend));
                            }
                        }
                        needs_redraw = true;
                    }

                    // ── Home ─────────────────────────────────────────────
                    Key::Named(NamedKey::Home) => {
                        match self.active_view {
                            SidebarView::Board => {
                                let vr = board_visible_rows(backend);
                                self.board_tree.jump_to_edge(true, vr);
                            }
                            SidebarView::Machines => {
                                self.machine_sel = 0;
                                self.fix_machine_scroll(content_visible_rows(backend));
                            }
                        }
                        needs_redraw = true;
                    }

                    // ── End ──────────────────────────────────────────────
                    Key::Named(NamedKey::End) => {
                        match self.active_view {
                            SidebarView::Board => {
                                let vr = board_visible_rows(backend);
                                self.board_tree.jump_to_edge(false, vr);
                            }
                            SidebarView::Machines => {
                                let m = self.data.machines.len();
                                if m > 0 {
                                    self.machine_sel = m - 1;
                                }
                                self.fix_machine_scroll(content_visible_rows(backend));
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
                        self.board_tree.move_selection_by(jump, vr);
                        needs_redraw = true;
                    }

                    // ── PageUp (Board only) ───────────────────────────────
                    Key::Named(NamedKey::PageUp)
                        if self.active_view == SidebarView::Board =>
                    {
                        let vr = board_visible_rows(backend);
                        let jump = (vr.max(1) - 1).max(1) as isize;
                        self.board_tree.move_selection_by(-jump, vr);
                        needs_redraw = true;
                    }

                    // ── Enter / Space — expand/collapse group (Board only) ─
                    Key::Named(NamedKey::Enter) | Key::Char(' ')
                        if self.active_view == SidebarView::Board =>
                    {
                        self.toggle_selected_group();
                        needs_redraw = true;
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
        assert_eq!(SidebarView::Machines.next(), SidebarView::Board);
    }

    #[test]
    fn sidebar_view_index() {
        assert_eq!(SidebarView::Board.index(), 0);
        assert_eq!(SidebarView::Machines.index(), 1);
    }

    #[test]
    fn sidebar_view_label() {
        assert_eq!(SidebarView::Board.label(), "Board");
        assert_eq!(SidebarView::Machines.label(), "Machines");
    }

    #[test]
    fn sidebar_view_default_is_board() {
        assert_eq!(SidebarView::default(), SidebarView::Board);
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
}
