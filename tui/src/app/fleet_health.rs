//! Fleet-wide health aggregate — the always-visible status-bar indicator +
//! detail overlay (#1631, H-4).
//!
//! Mirrors `coord.health.aggregate`'s counting rule (one unit per machine's
//! already-rolled-up `severity`, plus one unit per fleet-scope check — see
//! that module's own doc comment) but can't literally call it — this is
//! Rust, that's Python, joined only by the wire shape both read
//! (`coord.health.fleet_snapshot.FleetHealthSnapshot.to_dict`, deserialized
//! into `BoardPayload::fleet_health` / `BoardData::fleet_health` in
//! `types.rs`). Keep the two counting rules in sync by hand if either ever
//! changes.
//!
//! **Renderers here consume `severity`/`headroom` verbatim.** Neither the
//! status-bar segment nor the detail overlay looks at a check's raw
//! numbers — every row already carries its own pre-decided severity string
//! (`FleetHealthCheckResult::severity` / `FleetMachineHealth::severity`),
//! chosen upstream by a Python probe. See `coord/health/models.py`'s
//! `CheckResult` doc comment for why that split matters.
//!
//! **Reached by right-click, not a status-bar letter row.** The status bar
//! has no click dispatch wired for its segments anywhere in this codebase
//! today (`StatusBarSegment::action_id` exists on the wire but nothing here
//! sets it), so the indicator itself is inert; right-clicking anywhere on
//! the status bar (`events.rs`'s Right-MouseDown handler, `ctx.
//! in_status_bar`) opens a one-item context menu whose accelerator is shown
//! *inside* the menu (`ContextMenuItem::with_shortcut`) — the issue's own
//! framing for why this isn't "a status-bar letter row the operator has to
//! already know about".
//!
//! **Import pattern:** `use super::*` is intentional — see `escalation.rs`/
//! `drive.rs`/`audit.rs` for the same rationale.
#[allow(unused_imports)]
use super::*;

/// Mirrors `coord.health.models.Severity` — same four states, same rank
/// order. Declaration order IS the rank order (`#[derive(Ord)]`): `Unknown`
/// outranks `Ok` (a missing signal is never mistaken for a healthy one) but
/// never outranks `Warn`/`Crit` (never pages).
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Default)]
pub(crate) enum FleetSeverity {
    #[default]
    Ok,
    Unknown,
    Warn,
    Crit,
}

impl FleetSeverity {
    fn from_wire(s: &str) -> Self {
        match s {
            "ok" => FleetSeverity::Ok,
            "warn" => FleetSeverity::Warn,
            "crit" => FleetSeverity::Crit,
            // "unknown", empty, or anything unrecognised — never silently OK
            // (#1485: absence must never read as healthy).
            _ => FleetSeverity::Unknown,
        }
    }

    /// `OK` / `WARN` / `CRIT` / `?` — mirrors `Severity.label` in
    /// `coord/health/models.py`.
    pub(crate) fn label(self) -> &'static str {
        match self {
            FleetSeverity::Ok => "OK",
            FleetSeverity::Unknown => "?",
            FleetSeverity::Warn => "WARN",
            FleetSeverity::Crit => "CRIT",
        }
    }

    /// `(fg, bg)` for the status-bar segment and overlay rows. `Unknown` is
    /// deliberately NOT a dimmer shade of the `Ok` colours — it must read as
    /// "no signal", visually distinct from "fine", not a fainter version of
    /// it (#1485 / the issue's own "stale or unknown ... renders visibly
    /// differently" acceptance bullet).
    pub(crate) fn colors(self) -> (Color, Color) {
        match self {
            FleetSeverity::Ok => (Color::rgb(190, 235, 190), Color::rgb(20, 60, 20)),
            FleetSeverity::Unknown => (Color::rgb(210, 210, 220), Color::rgb(70, 70, 80)),
            FleetSeverity::Warn => (Color::rgb(255, 210, 100), Color::rgb(70, 45, 10)),
            FleetSeverity::Crit => (Color::rgb(255, 255, 255), Color::rgb(150, 30, 30)),
        }
    }
}

/// Aggregate verdict across every machine + fleet-scope check — mirrors
/// `coord.health.aggregate.FleetHealthSummary`.
#[derive(Clone, Copy, Debug, Default)]
pub(crate) struct FleetHealthSummary {
    pub(crate) worst: FleetSeverity,
    pub(crate) ok: usize,
    pub(crate) unknown: usize,
    pub(crate) warn: usize,
    pub(crate) crit: usize,
}

/// Aggregate a `FleetHealthBlock` — one unit per machine (its own
/// already-rolled-up `severity`) plus one unit per fleet-scope check. See
/// the module doc comment for why this can't be fewer/more granular.
pub(crate) fn summarize_fleet_health(block: &FleetHealthBlock) -> FleetHealthSummary {
    let mut summary = FleetHealthSummary::default();
    for m in &block.machine_health {
        tally(&mut summary, FleetSeverity::from_wire(&m.severity));
    }
    for c in &block.fleet_checks {
        tally(&mut summary, FleetSeverity::from_wire(&c.severity));
    }
    summary
}

fn tally(summary: &mut FleetHealthSummary, sev: FleetSeverity) {
    match sev {
        FleetSeverity::Ok => summary.ok += 1,
        FleetSeverity::Unknown => summary.unknown += 1,
        FleetSeverity::Warn => summary.warn += 1,
        FleetSeverity::Crit => summary.crit += 1,
    }
    if sev > summary.worst {
        summary.worst = sev;
    }
}

/// `FLEET: <state>  (coord health for detail)` — mirrors
/// `coord.health.aggregate.render_fleet_footer`'s text exactly (same
/// ascending non-OK enumeration, same "OK states its OK-ness" rule) so the
/// CLI footer and this status-bar segment never say different things about
/// the same fleet.
pub(crate) fn fleet_footer_text(summary: &FleetHealthSummary) -> String {
    if summary.worst == FleetSeverity::Ok {
        return "FLEET: OK  (coord health for detail)".to_string();
    }
    let mut parts: Vec<String> = Vec::new();
    if summary.unknown > 0 {
        parts.push(format!("{} {}", FleetSeverity::Unknown.label(), summary.unknown));
    }
    if summary.warn > 0 {
        parts.push(format!("{} {}", FleetSeverity::Warn.label(), summary.warn));
    }
    if summary.crit > 0 {
        parts.push(format!("{} {}", FleetSeverity::Crit.label(), summary.crit));
    }
    format!("FLEET: {}  (coord health for detail)", parts.join(", "))
}

/// `"  (2m ago)"` / `"  (43s ago)"` / `""` when there's no timestamp to
/// measure from. Purely presentational — never feeds back into severity.
fn age_string(checked_at: Option<f64>) -> String {
    let Some(ts) = checked_at else {
        return String::new();
    };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(ts);
    let age = (now - ts).max(0.0);
    if age < 90.0 {
        format!("  ({}s ago)", age as u64)
    } else {
        format!("  ({}m ago)", (age / 60.0) as u64)
    }
}

impl CoordApp {
    pub(crate) fn fleet_health_summary(&self) -> FleetHealthSummary {
        summarize_fleet_health(&self.data.fleet_health)
    }

    /// The persistent status-bar indicator — ALWAYS present regardless of
    /// severity (#1631: "OK states its OK-ness rather than printing
    /// nothing" — silence is indistinguishable from a broken check). Carries
    /// no `action_id`: this codebase has no status-bar click dispatch wired
    /// up anywhere (see the module doc comment) — reached by right-click
    /// instead.
    pub(crate) fn fleet_health_status_bar_segment(&self) -> StatusBarSegment {
        let summary = self.fleet_health_summary();
        let (fg, bg) = summary.worst.colors();
        StatusBarSegment {
            text: format!(" {} ", fleet_footer_text(&summary)),
            fg,
            bg,
            bold: true,
            action_id: None,
        }
    }

    /// Right-click-on-the-status-bar menu: a single item, its accelerator
    /// shown *inside* the menu — not a status-bar letter row the operator
    /// has to already know about (the issue's own framing).
    pub(crate) fn context_menu_items_for_fleet_health(&self) -> Vec<ContextMenuItem> {
        vec![
            ContextMenuItem::action("open-fleet-health-detail", "Fleet health…")
                .with_shortcut("h"),
        ]
    }

    pub(crate) fn open_fleet_health_overlay(&mut self) {
        self.fleet_health_overlay_open = true;
    }

    pub(crate) fn close_fleet_health_overlay(&mut self) {
        self.fleet_health_overlay_open = false;
    }

    /// Popup geometry — same inset formula as `plans.rs`'s (private)
    /// `help_overlay_rect`; duplicated rather than reused because that
    /// helper isn't `pub(crate)` and this overlay isn't Plans-specific.
    fn fleet_health_overlay_rect(main: Rect) -> Rect {
        let w = (main.width - 4.0).max(20.0).min(main.width);
        let h = (main.height - 2.0).max(10.0).min(main.height);
        let x = main.x + (main.width - w) * 0.5;
        let y = main.y + (main.height - h) * 0.5;
        Rect::new(x, y, w, h)
    }

    /// Paint the detail overlay — a no-op when closed. Grouped by machine
    /// (acceptance: "the full check list grouped by machine, each with its
    /// headroom string and its measurement age"); a stale/unknown machine's
    /// header renders in `FleetSeverity::Unknown`'s colours, visibly
    /// distinct from a healthy one's green.
    pub(crate) fn render_fleet_health_overlay(&self, backend: &mut dyn Backend, main: Rect) {
        if !self.fleet_health_overlay_open {
            return;
        }
        let block = &self.data.fleet_health;
        let mut items: Vec<ListItem> = Vec::new();

        if block.machine_health.is_empty() && block.fleet_checks.is_empty() {
            items.push(muted_row("No fleet-health data on this connection."));
        }

        for m in &block.machine_health {
            let sev = FleetSeverity::from_wire(&m.severity);
            let (fg, _) = sev.colors();
            // `m.state` ("online"/"offline"/"unknown", from the daemon's own
            // reachability probe) is a DIFFERENT signal than `severity`
            // (the health-check verdict) — a machine can be online with a
            // CRIT disk, or offline with a last-known-OK disk. Surfacing
            // both is what makes "stale/unknown" mean something concrete
            // rather than a bare adjective.
            let distinct_note = if matches!(sev, FleetSeverity::Unknown) || m.stale {
                format!("  — stale/unknown (state: {})", state_label(&m.state))
            } else {
                String::new()
            };
            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(
                        format!("▸ {}  [{}]{}", m.machine, sev.label(), distinct_note),
                        fg,
                    )],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Header,
            });
            if m.results.is_empty() {
                items.push(muted_row(&format!(
                    "    (no checks reported){}",
                    age_string(m.checked_at)
                )));
            }
            for r in &m.results {
                items.extend(check_result_rows(r, age_string(m.checked_at).as_str()));
            }
        }

        if !block.fleet_checks.is_empty() {
            items.push(ListItem {
                text: StyledText::plain("▸ fleet".to_string()),
                icon: None,
                detail: None,
                decoration: Decoration::Header,
            });
            for r in &block.fleet_checks {
                items.extend(check_result_rows(r, ""));
            }
        }

        items.push(muted_row("(Esc to close)"));

        let rect = Self::fleet_health_overlay_rect(main);
        let total = items.len();
        backend.draw_list(
            rect,
            &ListView {
                id: WidgetId::new("fleet-health-overlay"),
                title: Some(StyledText::plain("Fleet health".to_string())),
                items,
                selected_idx: 0,
                scroll_offset: 0,
                has_focus: false,
                bordered: true,
                h_scroll: 0,
                max_content_width: None,
                show_v_scrollbar: (total as f32) > (rect.height.max(1.0)),
            },
        );
    }
}

fn muted_row(text: &str) -> ListItem {
    ListItem {
        text: StyledText {
            spans: vec![StyledSpan::with_fg(text.to_string(), Color::rgb(140, 140, 150))],
        },
        icon: None,
        detail: None,
        decoration: Decoration::Muted,
    }
}

/// `m.state` reads empty for a machine this daemon has never polled at all
/// (absent DB row, per `coord.state.load_machine_health`'s doc comment) —
/// render that as "never polled" rather than a blank field.
fn state_label(state: &str) -> &str {
    if state.is_empty() {
        "never polled"
    } else {
        state
    }
}

/// One `<SEVERITY> <label>  <headroom>  [<threshold>]  [<age>]` row, plus a
/// muted `<detail>` continuation row when the check is non-OK and left one
/// (mirrors `coord.health.render.render_result`'s "the threshold reminder
/// is only useful next to a number that is near it" / "verbose or not OK"
/// rules — this overlay IS the "expand for detail" surface, so it always
/// shows detail for a non-OK row rather than gating it behind a flag).
fn check_result_rows(r: &FleetHealthCheckResult, age: &str) -> Vec<ListItem> {
    let sev = FleetSeverity::from_wire(&r.severity);
    let (fg, _) = sev.colors();
    let label = if r.label.is_empty() { &r.title } else { &r.label };
    let mut line = format!("    {:<5} {}  {}", sev.label(), label, r.headroom);
    if !r.threshold.is_empty() && sev != FleetSeverity::Ok {
        line.push_str("  ");
        line.push_str(&r.threshold);
    }
    line.push_str(age);
    let mut rows = vec![ListItem {
        text: StyledText {
            spans: vec![StyledSpan::with_fg(line, fg)],
        },
        icon: None,
        detail: None,
        decoration: Decoration::Normal,
    }];
    if !r.detail.is_empty() && sev != FleetSeverity::Ok {
        rows.push(muted_row(&format!("        {}", r.detail)));
    }
    rows
}
