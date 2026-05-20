//! coord-tui — TUI dashboard for claude-coordinator.
//!
//! Three-panel layout rendered with quadraui:
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
//! - `~/.coord/agent_state.json` — assignments managed by the local agent
//! - `~/.coord/dispatched.json`  — cross-machine dispatch history (machine names)
//!
//! **Agent endpoint:** TCP probe on port 7433 to determine machine reachability.
//!
//! **Keys:** `j`/`↓` down, `k`/`↑` up, `Home`/`End`, `r` force-refresh, `q`/`Esc` quit.

use std::io;
use std::net::{TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use ratatui::backend::CrosstermBackend;
use ratatui::crossterm::event::{DisableMouseCapture, EnableMouseCapture};
use ratatui::crossterm::execute;
use ratatui::crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use ratatui::Terminal;

use serde::Deserialize;

use quadraui::tui::TuiBackend;
use quadraui::{
    AppLogic, Backend, Color, Decoration, Key, ListItem, ListView, NamedKey, Reaction, Rect,
    Split, SplitDirection, StatusBar, StatusBarSegment, StyledSpan, StyledText, UiEvent, Viewport,
    WidgetId,
};

// ─── JSON deserialization types ───────────────────────────────────────────────

#[derive(Deserialize, Default)]
struct AgentStateJson {
    #[serde(default)]
    machine: String,
    #[serde(default)]
    assignments: Vec<AgentAssignmentJson>,
}

#[derive(Deserialize)]
struct AgentAssignmentJson {
    id: String,
    spec: AssignmentSpecJson,
    status: String,
    #[serde(default)]
    branch: Option<String>,
    #[serde(default)]
    started_at: Option<f64>,
    #[serde(default)]
    finished_at: Option<f64>,
    #[serde(default)]
    exit_code: Option<i32>,
}

#[derive(Deserialize)]
struct AssignmentSpecJson {
    repo_name: String,
    issue_number: u64,
    issue_title: String,
    #[serde(default)]
    model: Option<String>,
    #[serde(rename = "type", default)]
    assignment_type: Option<String>,
}

#[derive(Deserialize, Default)]
struct DispatchedEntryJson {
    #[serde(default)]
    machine_name: String,
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
    started_at: Option<f64>,
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
        match self.started_at {
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

/// Truncate `s` to `max` bytes (ASCII-safe; won't split multi-byte chars
/// because all our strings are ASCII identifiers and short titles).
fn trunc(s: &str, max: usize) -> &str {
    if s.len() <= max {
        s
    } else {
        &s[..max]
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
                StyledSpan::with_fg(&format!(" {:12} ", key), key_color),
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

    // ── agent_state.json (local machine assignments) ──────────────────
    let state: AgentStateJson = std::fs::read_to_string(dir.join("agent_state.json"))
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default();

    let local = state.machine.clone();

    let mut assignments: Vec<Assignment> = state
        .assignments
        .into_iter()
        .map(|a| {
            Assignment {
                id: a.id,
                repo: a.spec.repo_name,
                issue_number: a.spec.issue_number,
                issue_title: a.spec.issue_title,
                machine: local.clone(),
                status: a.status,
                branch: a.branch,
                model: a.spec.model,
                started_at: a.started_at,
                finished_at: a.finished_at,
                exit_code: a.exit_code,
                assignment_type: a.spec.assignment_type,
            }
        })
        .collect();

    // Sort: running first, then failed, then done (most recent first within groups)
    assignments.sort_by(|a, b| {
        let rank = |s: &str| match s {
            "running" => 0u8,
            "failed" => 1,
            "done" => 2,
            _ => 3,
        };
        rank(&a.status).cmp(&rank(&b.status)).then_with(|| {
            b.started_at
                .partial_cmp(&a.started_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        })
    });

    // ── dispatched.json (extracts additional machine names) ───────────
    let mut machine_names: Vec<String> = if local.is_empty() {
        vec![]
    } else {
        vec![local.clone()]
    };

    if let Ok(s) = std::fs::read_to_string(dir.join("dispatched.json")) {
        if let Ok(entries) = serde_json::from_str::<Vec<DispatchedEntryJson>>(&s) {
            for e in entries {
                if !e.machine_name.is_empty() && !machine_names.contains(&e.machine_name) {
                    machine_names.push(e.machine_name);
                }
            }
        }
    }

    if machine_names.is_empty() && !local.is_empty() {
        machine_names.push(local.clone());
    }

    // ── Machine reachability probes ────────────────────────────────────
    // Spawn probes concurrently; collect within a 250 ms budget.
    let probes: Vec<(String, std::sync::mpsc::Receiver<bool>)> = machine_names
        .iter()
        .map(|name| {
            use std::sync::mpsc;
            let host = name.clone();
            let (tx, rx) = mpsc::channel();
            std::thread::spawn(move || {
                let _ = tx.send(tcp_probe(&host, 7433));
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

    BoardData {
        local_machine: local,
        assignments,
        machines,
    }
}

// ─── Dashboard app ────────────────────────────────────────────────────────────

struct Dashboard {
    data: BoardData,
    board_sel: usize,
    board_scroll: usize,
    refreshed_at: Instant,
}

impl Dashboard {
    fn new() -> Self {
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
                        StyledSpan::with_fg(&a.age_str(), Color::rgb(100, 100, 100)),
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
            title: Some(StyledText::plain(&format!(" BOARD ({} assignments) ", n))),
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
            title: Some(StyledText::plain(&format!(" MACHINES ({}) ", n))),
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

                if let (Some(start), Some(end)) = (a.started_at, a.finished_at) {
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

impl AppLogic for Dashboard {
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

    fn handle(&mut self, event: UiEvent, backend: &mut dyn Backend) -> Reaction {
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
                        return Reaction::Redraw;
                    }

                    Key::Char('k') | Key::Named(NamedKey::Up) => {
                        if self.board_sel > 0 {
                            self.board_sel -= 1;
                        }
                        self.fix_scroll(board_visible_rows(backend));
                        return Reaction::Redraw;
                    }

                    Key::Named(NamedKey::Home) => {
                        self.board_sel = 0;
                        self.fix_scroll(board_visible_rows(backend));
                        return Reaction::Redraw;
                    }

                    Key::Named(NamedKey::End) => {
                        if n > 0 {
                            self.board_sel = n - 1;
                        }
                        self.fix_scroll(board_visible_rows(backend));
                        return Reaction::Redraw;
                    }

                    Key::Char('r') => {
                        self.refresh();
                        return Reaction::Redraw;
                    }

                    _ => {}
                }
            }

            UiEvent::WindowResized { .. } => return Reaction::Redraw,

            _ => {}
        }

        Reaction::Continue
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

// ─── Custom run loop ──────────────────────────────────────────────────────────
//
// We implement our own loop (rather than using `quadraui::tui::run`) so we
// can interleave timer-based auto-refresh with the event drain without
// waiting for a keyboard event to trigger it.

/// Auto-refresh the board data every 5 seconds.
const REFRESH_EVERY: Duration = Duration::from_secs(5);

/// Maximum time to block in `wait_events`. Kept short so the timer
/// fires within ~200 ms of its deadline.
const POLL_TIMEOUT: Duration = Duration::from_millis(200);

fn main() -> io::Result<()> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen, EnableMouseCapture)?;

    let crossterm_backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(crossterm_backend)?;
    terminal.clear()?;

    let mut backend = TuiBackend::new();
    let mut app = Dashboard::new();

    // Wrap in catch_unwind so we restore the terminal even on panic.
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        run_loop(&mut terminal, &mut backend, &mut app)
    }));

    // Restore terminal unconditionally.
    let _ = disable_raw_mode();
    let _ = execute!(
        terminal.backend_mut(),
        DisableMouseCapture,
        LeaveAlternateScreen
    );
    let _ = terminal.show_cursor();

    match result {
        Ok(r) => r,
        Err(p) => std::panic::resume_unwind(p),
    }
}

fn run_loop(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    backend: &mut TuiBackend,
    app: &mut Dashboard,
) -> io::Result<()> {
    let mut needs_redraw = true;

    loop {
        // ── Timer: auto-refresh every REFRESH_EVERY ───────────────────
        if app.refreshed_at.elapsed() >= REFRESH_EVERY {
            app.refresh();
            needs_redraw = true;
        }

        // ── Draw one frame if dirty ───────────────────────────────────
        if needs_redraw {
            let size = terminal.size()?;
            backend.begin_frame(Viewport::new(size.width as f32, size.height as f32, 1.0));
            terminal.draw(|frame| {
                backend.enter_frame_scope(frame, |b| {
                    app.render(b, ());
                });
            })?;
            backend.end_frame();
            needs_redraw = false;
        }

        // ── Drain events (blocks up to POLL_TIMEOUT) ──────────────────
        let events = backend.wait_events(POLL_TIMEOUT);
        for event in events {
            match app.handle(event, backend) {
                Reaction::Continue => {}
                Reaction::Redraw => needs_redraw = true,
                Reaction::Exit => return Ok(()),
            }
        }
    }
}
