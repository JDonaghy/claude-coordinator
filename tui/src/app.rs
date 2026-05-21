//! Backend-neutral app logic for coord-tui.
//!
//! [`CoordApp`] implements [`quadraui::AppLogic`] using only the
//! backend-neutral trait surface (`draw_list`, `draw_split`,
//! `draw_status_bar`). No ratatui or crossterm symbols appear here —
//! those live exclusively in the TUI and GTK shim entry points.
//!
//! ## Layout
//!
//! ```text
//! ┌──────────────────────────┬─────────────────────────────────────────┐
//! │ BOARD (12 assignments)   │ DETAIL — claude-coordinator #115        │
//! │ #115  claude-coord  RUN  │                                         │
//! │ #110  claude-coord  DONE │  TUI dashboard Phase 1: static bo…      │
//! │ #67   claude-coord  DONE │                                         │
//! │ #931  vimcode       FAIL │  ID           6b2670e37e1b              │
//! │                          │  Machine      dellserver                │
//! ├──────────────────────────│  Status       RUN                       │
//! │ MACHINES (1)             │  Model        sonnet                    │
//! │ ● dellserver (local)  1  │  Branch       (none yet)               │
//! │ ○ elitebook         idle │  Age          14m                       │
//! └──────────────────────────┴─────────────────────────────────────────┘
//! │ coord-tui  ↻ 3s           j/k=nav  r=refresh  q=quit             │
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
    AppLogic, Backend, Color, Decoration, Key, ListItem, ListView, NamedKey, Reaction, Rect,
    Split, SplitDirection, StatusBar, StatusBarSegment, StyledSpan, StyledText, UiEvent, WidgetId,
};

// ─── Auto-refresh interval ────────────────────────────────────────────────────

/// Reload board data every 5 seconds.
const REFRESH_EVERY: Duration = Duration::from_secs(5);

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
    board_sel: usize,
    board_scroll: usize,
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
        Self {
            data,
            board_sel: 0,
            board_scroll: 0,
            refreshed_at: Instant::now(),
        }
    }

    fn refresh(&mut self) {
        self.data = load_data();
        self.refreshed_at = Instant::now();
        let n = self.data.assignments.len();
        if n > 0 {
            self.board_sel = self.board_sel.min(n - 1);
        } else {
            self.board_sel = 0;
        }
    }

    fn selected(&self) -> Option<&Assignment> {
        self.data.assignments.get(self.board_sel)
    }

    /// Clamp `board_scroll` so that `board_sel` is inside the visible window.
    fn fix_scroll(&mut self, visible: usize) {
        if visible == 0 {
            return;
        }
        if self.board_sel < self.board_scroll {
            self.board_scroll = self.board_sel;
        } else if self.board_sel >= self.board_scroll + visible {
            self.board_scroll = self.board_sel + 1 - visible;
        }
    }

    // ── ListView builders ────────────────────────────────────────────────

    fn board_list(&self, has_focus: bool) -> ListView {
        let items: Vec<ListItem> = self
            .data
            .assignments
            .iter()
            .map(|a| {
                let sc = a.status_color();
                // Columns: issue#  repo(left-padded)  STATUS  (age right-aligned via detail)
                let issue = format!("#{:<5}", a.issue_number);
                let repo = format!("{:<18}", trunc(&a.repo, 18));
                let st = a.status_label();
                let text = StyledText {
                    spans: vec![
                        StyledSpan::with_fg(&issue, Color::rgb(150, 150, 240)),
                        StyledSpan::plain(&repo),
                        StyledSpan::with_fg(st, sc),
                    ],
                };
                let short_id = trunc(&a.id, 8);
                let detail = Some(StyledText {
                    spans: vec![
                        StyledSpan::with_fg(short_id, Color::rgb(90, 90, 110)),
                        StyledSpan::with_fg(" · ", Color::rgb(60, 60, 70)),
                        StyledSpan::with_fg(a.age_str(), Color::rgb(100, 100, 100)),
                    ],
                });
                ListItem {
                    text,
                    icon: None,
                    detail,
                    decoration: if a.status == "failed" {
                        Decoration::Error
                    } else {
                        Decoration::Normal
                    },
                }
            })
            .collect();

        let n = self.data.assignments.len();
        ListView {
            id: WidgetId::new("board"),
            title: Some(StyledText::plain(format!(" BOARD ({} assignments) ", n))),
            items,
            selected_idx: if n > 0 { self.board_sel } else { 0 },
            scroll_offset: self.board_scroll,
            has_focus,
            bordered: false,
        }
    }

    fn machines_list(&self) -> ListView {
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
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: false,
            bordered: false,
        }
    }

    fn detail_list(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();

        match self.selected() {
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

        let title = match self.selected() {
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

    fn status_bar(&self) -> StatusBar {
        let since = self.refreshed_at.elapsed().as_secs();
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
                    text: format!(" ↻ {}s ", since),
                    fg: Color::rgb(140, 140, 140),
                    bg: Color::rgb(30, 30, 40),
                    bold: false,
                    action_id: None,
                },
            ],
            right_segments: vec![StatusBarSegment {
                text: " j/k=nav  r=refresh  q=quit ".to_string(),
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

        // ── Outer split: 35% left column | 65% right detail ──────────
        let outer_split = Split {
            id: WidgetId::new("outer"),
            direction: SplitDirection::Horizontal,
            ratio: 0.35,
            first_min: 0.0,
            second_min: 0.0,
        };
        let outer = backend.draw_split(main_rect, &outer_split);

        // ── Left column: 65% board | 35% machines ────────────────────
        let left_split = Split {
            id: WidgetId::new("left"),
            direction: SplitDirection::Vertical,
            ratio: 0.65,
            first_min: 0.0,
            second_min: 0.0,
        };
        let left = backend.draw_split(outer.first_bounds, &left_split);

        // Draw the three panels
        backend.draw_list(left.first_bounds, &self.board_list(true));
        backend.draw_list(left.second_bounds, &self.machines_list());
        backend.draw_list(outer.second_bounds, &self.detail_list());

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
        let n = self.data.assignments.len();

        match event {
            UiEvent::KeyPressed { key, .. } => {
                match key {
                    Key::Char('q') | Key::Named(NamedKey::Escape) => return Reaction::Exit,

                    Key::Char('j') | Key::Named(NamedKey::Down) => {
                        if n > 0 && self.board_sel + 1 < n {
                            self.board_sel += 1;
                        }
                        self.fix_scroll(board_visible_rows(backend));
                        needs_redraw = true;
                    }

                    Key::Char('k') | Key::Named(NamedKey::Up) => {
                        if self.board_sel > 0 {
                            self.board_sel -= 1;
                        }
                        self.fix_scroll(board_visible_rows(backend));
                        needs_redraw = true;
                    }

                    Key::Named(NamedKey::Home) => {
                        self.board_sel = 0;
                        self.fix_scroll(board_visible_rows(backend));
                        needs_redraw = true;
                    }

                    Key::Named(NamedKey::End) => {
                        if n > 0 {
                            self.board_sel = n - 1;
                        }
                        self.fix_scroll(board_visible_rows(backend));
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

/// Estimate the number of visible rows in the board panel.
///
/// Board occupies 65% of the left column height, minus the title row.
/// Left column is the full terminal height minus the status bar row.
fn board_visible_rows(backend: &dyn Backend) -> usize {
    let vp = backend.viewport();
    let lh = backend.line_height();
    if lh <= 0.0 {
        return 10;
    }
    let main_h = vp.height - lh; // minus status bar
    let board_h = main_h * 0.65; // 65% for board panel
    let content_h = (board_h - lh).max(0.0); // minus title row
    (content_h / lh) as usize
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

    // ── fix_scroll ─────────────────────────────────────────────────────────────

    fn make_app(sel: usize, scroll: usize) -> CoordApp {
        CoordApp {
            data: BoardData::default(),
            board_sel: sel,
            board_scroll: scroll,
            refreshed_at: Instant::now(),
        }
    }

    #[test]
    fn fix_scroll_within_visible_window() {
        let mut d = make_app(2, 0);
        d.fix_scroll(10);
        // sel=2 is inside [0, 10) → scroll unchanged
        assert_eq!(d.board_scroll, 0);
    }

    #[test]
    fn fix_scroll_selection_past_end_of_window() {
        let mut d = make_app(15, 0);
        d.fix_scroll(10);
        // sel=15 >= scroll(0)+visible(10) → scroll = 15 + 1 - 10 = 6
        assert_eq!(d.board_scroll, 6);
    }

    #[test]
    fn fix_scroll_selection_before_scroll_offset() {
        let mut d = make_app(0, 5);
        d.fix_scroll(10);
        // sel=0 < scroll=5 → scroll snaps up to sel
        assert_eq!(d.board_scroll, 0);
    }

    #[test]
    fn fix_scroll_zero_visible_rows_is_noop() {
        let mut d = make_app(0, 0);
        d.fix_scroll(0); // visible == 0 → early return
        assert_eq!(d.board_scroll, 0);
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
