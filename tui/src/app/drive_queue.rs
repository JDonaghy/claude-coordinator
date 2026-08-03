//! The operator-declared `coord drive` work queue (#1750 DQ-3 / #1755) —
//! an always-visible status-bar segment, a right-click detail overlay, and
//! "Add to drive queue" on the Pipeline row menu.
//!
//! The TUI is where the operator reads the pipeline and forms the intent
//! "that one next"; before this, acting on that intent meant leaving the TUI
//! for a shell, and the epic's alert requirement ("if nothing can start,
//! alert the operator") had no surface at all.
//!
//! **Read path is entirely server-derived.** `self.data.drive_queue` is a raw
//! dump of the daemon's `drive_queue` table (`/board`'s `drive_queue` key,
//! see [`BoardDriveQueueEntry`]); nothing here recomputes a row's `state` —
//! DQ-2's tick decides `waiting`/`running`/`blocked`/`done` and this module
//! consumes that verbatim, exactly the way `fleet_health.rs` consumes a
//! probe's `severity`. The one thing derived locally is the *aggregate*
//! sentence in the status bar ([`drive_queue_status_text`]), which is a pure
//! function of the rows so it is directly testable without a driver.
//!
//! **Write path is `coord drive-queue …` through the spawn-and-toast seam**
//! (`command_runner.spawn_queued` + [`CoordApp::push_toast`], the same
//! posture as `milestone_dag.rs` / `escalation.rs`) — no direct DB access,
//! and no client-side mutation of `data.drive_queue` beyond the optimistic
//! removal `escalation.rs` already precedents.
//!
//! **Reached by right-click, not a status-bar letter row** — see
//! `fleet_health.rs`'s module doc comment for why (`StatusBarSegment::
//! action_id` exists on the wire but this codebase wires no per-segment
//! click dispatch, and adding one was explicitly out of scope for #1755).
//! Right-clicking anywhere on the status bar opens the shared
//! [`ContextMenuTarget::FleetHealth`] menu, which since #1755 carries a
//! "Drive queue…" item alongside "Fleet health…".
//!
//! **Import pattern:** `use super::*` is intentional — see `fleet_health.rs`
//! / `escalation.rs` / `drive.rs` for the same rationale.
#[allow(unused_imports)]
use super::*;

/// Wire values of `drive_queue.state` this module reasons about. Everything
/// else (an unrecognised state from a newer daemon) counts as neither
/// eligible nor blocked and is rendered verbatim — never silently folded
/// into a healthy bucket.
pub(crate) const QUEUE_STATE_WAITING: &str = "waiting";
pub(crate) const QUEUE_STATE_RUNNING: &str = "running";
pub(crate) const QUEUE_STATE_BLOCKED: &str = "blocked";
pub(crate) const QUEUE_STATE_DONE: &str = "done";

/// #1755: pending "Add to drive queue after…" text input. Mirrors
/// `PendingMilestoneRowInput`'s single-buffer shape (Enter submits, Esc
/// cancels) rather than a full form — the whole payload is a short list of
/// issue numbers.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct PendingDriveQueueAfter {
    /// Coord-local repo of the row being queued — also the repo bare issue
    /// numbers in `buf` resolve against (`parse_after_spec`'s rule).
    pub(crate) repo_name: String,
    pub(crate) issue_number: u64,
    pub(crate) buf: String,
}

/// Severity of the aggregate queue reading, in ascending rank order
/// (declaration order IS the rank, `#[derive(Ord)]` — same convention as
/// `FleetSeverity`). `Blocked` outranks `Stalled` per the issue's own
/// "blocked outranks stalled" rule.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Default)]
pub(crate) enum DriveQueueLevel {
    /// Nothing queued at all. Muted — an empty queue is not a problem, but
    /// it must still SAY it's empty (#1631's "OK states its OK-ness").
    #[default]
    Empty,
    /// Something is running and/or something is waiting behind it.
    Normal,
    /// Rows are waiting and not one of them can start. This is the epic's
    /// alert case: the queue looks busy but nothing will ever move without
    /// the operator.
    Stalled,
    /// At least one row is `blocked` — a hard stop DQ-2 already gave up on.
    Blocked,
}

impl DriveQueueLevel {
    /// `(fg, bg)` for the status-bar segment. Deliberately reuses the exact
    /// palette `FleetSeverity::colors` uses for ok/warn/crit so two adjacent
    /// segments never disagree about what amber means.
    pub(crate) fn colors(self) -> (Color, Color) {
        match self {
            // Muted grey-on-dark: present, legible, visibly *not* an alert.
            DriveQueueLevel::Empty => (Color::rgb(150, 150, 160), Color::rgb(30, 30, 40)),
            DriveQueueLevel::Normal => (Color::rgb(200, 220, 255), Color::rgb(40, 60, 90)),
            DriveQueueLevel::Stalled => (Color::rgb(255, 210, 100), Color::rgb(70, 45, 10)),
            DriveQueueLevel::Blocked => (Color::rgb(255, 255, 255), Color::rgb(150, 30, 30)),
        }
    }
}

/// Rows that still have work ahead of them — `done` entries are history and
/// must not inflate "N waiting" or keep the segment shouting forever.
fn is_pending(e: &BoardDriveQueueEntry) -> bool {
    e.state != QUEUE_STATE_DONE
}

/// Is this row's `after` list satisfied by the queue it sits in?
///
/// Deliberately a *local, conservative* read: the authoritative eligibility
/// decision belongs to DQ-2's tick (which also weighs board state, capacity
/// and machine routing). All this answers is the question the operator can
/// verify by eye — "is a pre-req of mine still sitting in this same queue,
/// unfinished?" — which is the only stall cause the TUI can name without
/// re-implementing the tick. A pre-req that isn't in the queue at all is
/// treated as satisfied (it may have landed long ago).
fn after_satisfied(entry: &BoardDriveQueueEntry, all: &[BoardDriveQueueEntry]) -> bool {
    entry.after.iter().all(|key| {
        !all.iter()
            .any(|other| other.key() == *key && other.state != QUEUE_STATE_DONE)
    })
}

/// Aggregate reading + counts behind [`drive_queue_status_text`]. Split out
/// so the segment's colour and its text can never be derived from two
/// different passes over the rows.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(crate) struct DriveQueueSummary {
    pub(crate) level: DriveQueueLevel,
    pub(crate) running: usize,
    pub(crate) waiting: usize,
    pub(crate) blocked: usize,
    /// Waiting rows whose in-queue pre-reqs are all satisfied — i.e. rows a
    /// tick could plausibly pick up. Zero-with-waiting-rows is the stall.
    pub(crate) eligible: usize,
}

/// Summarise the queue. Pure over the board rows — no clock, no self.
pub(crate) fn summarize_drive_queue(entries: &[BoardDriveQueueEntry]) -> DriveQueueSummary {
    let mut s = DriveQueueSummary::default();
    for e in entries.iter().filter(|e| is_pending(e)) {
        match e.state.as_str() {
            QUEUE_STATE_RUNNING => s.running += 1,
            QUEUE_STATE_BLOCKED => s.blocked += 1,
            QUEUE_STATE_WAITING => {
                s.waiting += 1;
                if after_satisfied(e, entries) {
                    s.eligible += 1;
                }
            }
            // An unrecognised state from a newer daemon: counted as pending
            // (it is not `done`) but never as waiting/eligible, so it can
            // neither trigger nor mask a stall.
            _ => {}
        }
    }
    s.level = if s.blocked > 0 {
        // Blocked outranks stalled — a hard stop is worse news than a queue
        // that is merely waiting for capacity.
        DriveQueueLevel::Blocked
    } else if s.waiting > 0 && s.eligible == 0 && s.running == 0 {
        // Nothing running AND nothing that could start: the epic's "if
        // nothing can start, alert the operator" case. A row running means
        // the queue IS moving, so waiting-behind-it is normal, not a stall.
        DriveQueueLevel::Stalled
    } else if s.running > 0 || s.waiting > 0 {
        DriveQueueLevel::Normal
    } else {
        DriveQueueLevel::Empty
    };
    s
}

/// The status-bar sentence. Always non-empty — an empty queue reads
/// `QUEUE: empty`, never nothing (#1631: silence is indistinguishable from a
/// broken feature).
///
/// ```text
/// QUEUE: empty
/// QUEUE: 1 running · 3 waiting
/// QUEUE: STALLED — 3 waiting, none eligible
/// QUEUE: BLOCKED 2 · 1 waiting
/// ```
pub(crate) fn drive_queue_status_text(entries: &[BoardDriveQueueEntry]) -> String {
    let s = summarize_drive_queue(entries);
    match s.level {
        DriveQueueLevel::Empty => "QUEUE: empty".to_string(),
        DriveQueueLevel::Blocked => {
            let mut out = format!("QUEUE: BLOCKED {}", s.blocked);
            if s.running > 0 {
                out.push_str(&format!(" · {} running", s.running));
            }
            if s.waiting > 0 {
                out.push_str(&format!(" · {} waiting", s.waiting));
            }
            out
        }
        DriveQueueLevel::Stalled => format!(
            "QUEUE: STALLED — {} waiting, none eligible",
            s.waiting
        ),
        DriveQueueLevel::Normal => {
            let mut parts: Vec<String> = Vec::new();
            if s.running > 0 {
                parts.push(format!("{} running", s.running));
            }
            if s.waiting > 0 {
                parts.push(format!("{} waiting", s.waiting));
            }
            format!("QUEUE: {}", parts.join(" · "))
        }
    }
}

impl CoordApp {
    /// The persistent status-bar segment — ALWAYS present, in every view,
    /// regardless of depth (see [`drive_queue_status_text`]). Carries no
    /// `action_id`: reached by right-clicking the status bar, per the module
    /// doc comment.
    pub(crate) fn drive_queue_status_bar_segment(&self) -> StatusBarSegment {
        let entries = &self.data.drive_queue;
        let level = summarize_drive_queue(entries).level;
        let (fg, bg) = level.colors();
        StatusBarSegment {
            text: format!(" {} ", drive_queue_status_text(entries)),
            fg,
            bg,
            // Bold only when it's news — an idle/normal queue shouldn't
            // compete with the fleet-health segment next to it for attention.
            bold: matches!(level, DriveQueueLevel::Stalled | DriveQueueLevel::Blocked),
            action_id: None,
        }
    }

    /// The status bar's "Drive queue…" item, appended to the shared
    /// right-click-the-status-bar menu alongside `context_menu_items_for_
    /// fleet_health`'s. Accelerator shown *inside* the menu, same framing.
    pub(crate) fn context_menu_items_for_drive_queue_segment(&self) -> Vec<ContextMenuItem> {
        vec![ContextMenuItem::action("open-drive-queue-detail", "Drive queue…").with_shortcut("q")]
    }

    /// The queue in run order (`position` ascending, `repo#issue` as a
    /// stable tiebreaker so two rows that momentarily share a position
    /// during a `move` never flicker past each other).
    pub(crate) fn drive_queue_entries(&self) -> Vec<&BoardDriveQueueEntry> {
        let mut rows: Vec<&BoardDriveQueueEntry> = self.data.drive_queue.iter().collect();
        rows.sort_by(|a, b| {
            a.position
                .cmp(&b.position)
                .then_with(|| a.repo_name.cmp(&b.repo_name))
                .then_with(|| a.issue_number.cmp(&b.issue_number))
        });
        rows
    }

    /// Is (repo, issue) already queued? Drives the Pipeline menu's
    /// "Add to drive queue" ⇄ "Remove from drive queue" swap.
    ///
    /// `done` rows count as *not* queued: the row is history, and re-queuing
    /// it is exactly what an operator re-running a merged-then-reopened issue
    /// wants (`coord drive-queue add` updates in place anyway).
    pub(crate) fn drive_queue_contains(&self, repo: &str, issue: u64) -> bool {
        self.data.drive_queue.iter().any(|e| {
            e.repo_name == repo && e.issue_number == issue as i64 && e.state != QUEUE_STATE_DONE
        })
    }

    /// The queued entry for (repo, issue), if any — including `done` rows,
    /// which the overlay still lists.
    pub(crate) fn drive_queue_entry_for(
        &self,
        repo: &str,
        issue: u64,
    ) -> Option<&BoardDriveQueueEntry> {
        self.data
            .drive_queue
            .iter()
            .find(|e| e.repo_name == repo && e.issue_number == issue as i64)
    }

    /// `(coord-local repo, issue)` for a Pipeline/Board right-click target,
    /// or `None` when the row has no issue or no repo mapping.
    ///
    /// Every `coord drive-queue` verb is repo-scoped, so a row whose
    /// `coord_repo` never resolved (an issue in a repo `coordinator.yml`
    /// doesn't know) has nothing this module can do with it — the menu items
    /// are suppressed for exactly that case (see
    /// [`Self::drive_queue_menu_items_for_pipeline_row`]), and this returning
    /// `None` is the belt-and-braces half of the same guard.
    pub(crate) fn pipeline_menu_repo_issue(
        &self,
        target: &ContextMenuTarget,
    ) -> Option<(String, u64)> {
        match target {
            ContextMenuTarget::PipelineRow {
                issue_number,
                repo_name,
                ..
            }
            | ContextMenuTarget::BoardRow {
                issue_number,
                repo_name,
                ..
            } => Some((repo_name.clone()?, (*issue_number)?)),
            _ => None,
        }
    }

    /// The drive-queue block of a Pipeline row's right-click menu.
    ///
    /// A row already in the queue offers **Remove** instead of the three Add
    /// variants — re-adding a queued row is a silent in-place update
    /// (`coord drive-queue add` upserts), which is not what "Add" reads as
    /// on a row that is already there.
    ///
    /// Suppressed entirely for a row with no issue number or no coord repo:
    /// `coord drive-queue add` takes `<repo> <issue>` and refuses a repo
    /// `coordinator.yml` has never heard of, so an unmapped row could only
    /// ever produce a failing command.
    ///
    /// `offer_add` is the "plausibly drivable" gate. `false` on an
    /// already-in-progress row: `coord drive` takes an issue from wherever
    /// it is to merged, so queuing a row that a worker (or another drive) is
    /// already mid-way through is a race, not an intent. **Remove is still
    /// offered in that case** — a row queued while New and since started
    /// must remain dequeueable from the row it's actually on.
    pub(crate) fn drive_queue_menu_items_for_pipeline_row(
        &self,
        issue_number: Option<u64>,
        repo_name: Option<&str>,
        offer_add: bool,
    ) -> Vec<ContextMenuItem> {
        let (Some(issue), Some(repo)) = (issue_number, repo_name) else {
            return Vec::new();
        };
        if repo.is_empty() {
            return Vec::new();
        }
        if self.drive_queue_contains(repo, issue) {
            // Name the slot it currently holds — the operator's next question
            // after "it's already queued" is always "where in the queue?",
            // and this is the only place a Pipeline row can answer it
            // without opening the overlay.
            let label = match self.drive_queue_entry_for(repo, issue) {
                Some(e) => format!("Remove from drive queue (position {})", e.position),
                None => "Remove from drive queue".to_string(),
            };
            return vec![ContextMenuItem::action("drive-queue-row-remove", &label)];
        }
        if !offer_add {
            return Vec::new();
        }
        let mut items = vec![ContextMenuItem::action("drive-queue-add", "Add to drive queue")];
        // "on…" — pin the drive to one machine. Built from the board's own
        // machine list so it can never offer a machine this coordinator
        // doesn't route to; omitted entirely when the board has none (an
        // empty submenu is a dead end, not an affordance).
        let machines: Vec<ContextMenuItem> = self
            .data
            .machines
            .iter()
            .map(|m| {
                ContextMenuItem::action(&format!("drive-queue-add-on:{}", m.name), &m.name)
            })
            .collect();
        if !machines.is_empty() {
            items.push(ContextMenuItem::parent("Add to drive queue on…", machines));
        }
        items.push(ContextMenuItem::action(
            "drive-queue-add-after",
            "Add to drive queue after…",
        ));
        items
    }

    // ── overlay ──────────────────────────────────────────────────────────

    pub(crate) fn open_drive_queue_overlay(&mut self) {
        self.drive_queue_overlay_open = true;
        let len = self.data.drive_queue.len();
        if self.drive_queue_sel >= len {
            self.drive_queue_sel = len.saturating_sub(1);
        }
    }

    pub(crate) fn close_drive_queue_overlay(&mut self) {
        self.drive_queue_overlay_open = false;
    }

    /// Move the overlay selection by `delta` rows, clamped. No wraparound —
    /// a queue is short and ordered, and wrapping past the tail hides how
    /// close to the end you are.
    pub(crate) fn drive_queue_move_selection(&mut self, delta: isize) {
        let len = self.drive_queue_entries().len();
        if len == 0 {
            self.drive_queue_sel = 0;
            return;
        }
        let next = (self.drive_queue_sel as isize + delta).clamp(0, len as isize - 1);
        self.drive_queue_sel = next as usize;
    }

    /// Popup geometry — same inset formula as `fleet_health_overlay_rect`.
    fn drive_queue_overlay_rect(main: Rect) -> Rect {
        let w = (main.width - 4.0).max(20.0).min(main.width);
        let h = (main.height - 2.0).max(10.0).min(main.height);
        let x = main.x + (main.width - w) * 0.5;
        let y = main.y + (main.height - h) * 0.5;
        Rect::new(x, y, w, h)
    }

    /// One `ListItem` per queue row, plus its optional muted continuation
    /// line. Returns `(items, row_targets)` where `row_targets[i]` is the
    /// queue index the painted row `i` belongs to (`None` for the trailing
    /// hint rows) — so [`Self::drive_queue_row_at`] hit-tests against the
    /// exact shape that was painted rather than re-deriving row arithmetic
    /// that could drift (the `plans_row_at` precedent).
    fn drive_queue_overlay_items(&self) -> (Vec<ListItem>, Vec<Option<usize>>) {
        let rows = self.drive_queue_entries();
        let mut items: Vec<ListItem> = Vec::new();
        let mut targets: Vec<Option<usize>> = Vec::new();

        if rows.is_empty() {
            items.push(dq_muted_row(
                "Drive queue is empty — right-click a Pipeline row → \"Add to drive queue\".",
            ));
            targets.push(None);
        }

        for (idx, e) in rows.iter().enumerate() {
            let selected = idx == self.drive_queue_sel;
            let (fg, _) = dq_state_colors(&e.state);
            let mut line = format!(
                "{} {:>2}  {:<28} {:<8}",
                if selected { "▸" } else { " " },
                e.position,
                e.key(),
                e.state,
            );
            match e.machine.as_deref() {
                Some(m) if !m.is_empty() => line.push_str(&format!("  on {m}")),
                // An unpinned row is the normal case, not a gap — say what
                // it means rather than leaving the column blank.
                _ => line.push_str("  (unpinned)"),
            }
            if let Some(s) = e.session_name.as_deref() {
                if !s.is_empty() {
                    line.push_str(&format!("  tmux:{s}"));
                }
            }
            if !e.after.is_empty() {
                line.push_str(&format!("  after {}", e.after.join(",")));
            }
            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(line, fg)],
                },
                icon: None,
                detail: None,
                decoration: if selected {
                    Decoration::Header
                } else {
                    Decoration::Normal
                },
            });
            targets.push(Some(idx));

            // Why a row is being passed over is the single most useful thing
            // in this overlay — always shown when there is anything to show.
            if e.deferrals > 0 || !e.last_reason.is_empty() || e.attempts > 0 {
                let mut bits: Vec<String> = Vec::new();
                if e.attempts > 0 {
                    bits.push(format!("attempts {}", e.attempts));
                }
                if e.deferrals > 0 {
                    bits.push(format!("deferrals {}", e.deferrals));
                }
                if !e.last_reason.is_empty() {
                    bits.push(format!("last: {}", e.last_reason));
                }
                items.push(dq_muted_row(&format!("      {}", bits.join("  ·  "))));
                targets.push(Some(idx));
            }
        }

        items.push(dq_muted_row(""));
        targets.push(None);
        items.push(dq_muted_row(
            "(j/k select · x remove · K/J move up/down · u unblock · right-click for the menu · Esc closes)",
        ));
        targets.push(None);
        (items, targets)
    }

    /// The `ListView` the overlay paints. Shared by the paint path and the
    /// hit-test so the two can never disagree about the layout.
    fn drive_queue_list_view(&self, items: Vec<ListItem>, total: usize, rect: Rect) -> ListView {
        ListView {
            id: WidgetId::new("drive-queue-overlay"),
            title: Some(StyledText::plain("Drive queue".to_string())),
            items,
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: true,
            bordered: true,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: (total as f32) > (rect.height.max(1.0)),
        }
    }

    /// Paint the overlay — a no-op when closed.
    pub(crate) fn render_drive_queue_overlay(&self, backend: &mut dyn Backend, main: Rect) {
        if !self.drive_queue_overlay_open {
            return;
        }
        let (items, _) = self.drive_queue_overlay_items();
        let rect = Self::drive_queue_overlay_rect(main);
        let total = items.len();
        let view = self.drive_queue_list_view(items, total, rect);
        backend.draw_list(rect, &view);
    }

    /// Queue index under `pos`, or `None` for the border / hint rows /
    /// outside. Built from the same painted item list as the render path.
    pub(crate) fn drive_queue_row_at(&self, pos: Point, main: Rect, lh: f32) -> Option<usize> {
        if !self.drive_queue_overlay_open {
            return None;
        }
        let (items, targets) = self.drive_queue_overlay_items();
        let rect = Self::drive_queue_overlay_rect(main);
        let total = items.len();
        let view = self.drive_queue_list_view(items, total, rect);
        let layout = view.layout(rect.width, rect.height, lh, |_| ListItemMeasure::new(lh));
        match layout.hit_test(pos.x - rect.x, pos.y - rect.y) {
            ListViewHit::Item(idx) => targets.get(idx).copied().flatten(),
            _ => None,
        }
    }

    /// True iff `pos` lands anywhere inside the open overlay.
    pub(crate) fn drive_queue_overlay_hit(&self, pos: Point, main: Rect) -> bool {
        if !self.drive_queue_overlay_open {
            return false;
        }
        let r = Self::drive_queue_overlay_rect(main);
        pos.x >= r.x && pos.x < r.x + r.width && pos.y >= r.y && pos.y < r.y + r.height
    }

    /// The right-click target for the overlay's currently-selected row.
    /// `None` for an empty queue (nothing to act on).
    pub(crate) fn drive_queue_context_target(&self) -> Option<ContextMenuTarget> {
        let rows = self.drive_queue_entries();
        let queue_len = rows.len();
        let e = rows.get(self.drive_queue_sel)?;
        Some(ContextMenuTarget::DriveQueueRow {
            repo_name: e.repo_name.clone(),
            issue_number: e.issue_number,
            state: e.state.clone(),
            position: e.position,
            queue_len,
        })
    }

    /// Per-row menu: Remove / Move up / Move down / Unblock. End-of-queue
    /// moves are DISABLED with a reason rather than silently no-op'ing
    /// (#1598's `disabled_because` precedent).
    pub(crate) fn context_menu_items_for_drive_queue_row(
        &self,
        state: &str,
        position: i64,
        queue_len: usize,
    ) -> Vec<ContextMenuItem> {
        let mut items = Vec::new();
        let mut up = ContextMenuItem::action("drive-queue-move-up", "Move up").with_shortcut("K");
        if position <= 0 {
            up = up.disabled_because("already first");
        }
        items.push(up);
        let mut down =
            ContextMenuItem::action("drive-queue-move-down", "Move down").with_shortcut("J");
        if queue_len == 0 || position as usize >= queue_len - 1 {
            down = down.disabled_because("already last");
        }
        items.push(down);
        // Only a `blocked` row has anything to unblock — offering it on a
        // waiting row would promise a state change that can't happen.
        if state == QUEUE_STATE_BLOCKED {
            items.push(
                ContextMenuItem::action("drive-queue-unblock", "Unblock").with_shortcut("u"),
            );
        }
        items.push(ContextMenuItem::separator());
        items.push(
            ContextMenuItem::action("drive-queue-remove", "Remove from queue").with_shortcut("x"),
        );
        items
    }

    // ── write path (`coord drive-queue …`) ───────────────────────────────

    /// `coord drive-queue add <repo> <issue> [--machine M] [--after K]…`.
    ///
    /// Optimistically appends to `data.drive_queue` so the status-bar count
    /// and the Pipeline menu's Add⇄Remove swap update on the next paint
    /// rather than on the next board poll (the `dispatch_dismiss_escalation`
    /// precedent). The next `/board` refresh overwrites it with the truth,
    /// including a rejected add simply vanishing again.
    pub(crate) fn dispatch_drive_queue_add(
        &mut self,
        repo: &str,
        issue: u64,
        machine: Option<&str>,
        after: &[String],
    ) {
        let issue_str = issue.to_string();
        let mut args: Vec<String> = vec![
            "drive-queue".to_string(),
            "add".to_string(),
            repo.to_string(),
            issue_str,
        ];
        if let Some(m) = machine.filter(|m| !m.is_empty()) {
            args.push("--machine".to_string());
            args.push(m.to_string());
        }
        for a in after.iter().filter(|a| !a.is_empty()) {
            args.push("--after".to_string());
            args.push(a.clone());
        }
        let arg_refs: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
        self.command_runner.spawn_queued(&arg_refs);

        if !self.drive_queue_contains(repo, issue) {
            let position = self
                .data
                .drive_queue
                .iter()
                .map(|e| e.position)
                .max()
                .map(|p| p + 1)
                .unwrap_or(0);
            self.data.drive_queue.push(BoardDriveQueueEntry {
                repo_name: repo.to_string(),
                issue_number: issue as i64,
                position,
                machine: machine.filter(|m| !m.is_empty()).map(str::to_string),
                after: after.to_vec(),
                state: QUEUE_STATE_WAITING.to_string(),
                ..BoardDriveQueueEntry::default()
            });
        }
        let pinned = match machine.filter(|m| !m.is_empty()) {
            Some(m) => format!(" on {m}"),
            None => String::new(),
        };
        let after_note = if after.is_empty() {
            String::new()
        } else {
            format!(" after {}", after.join(","))
        };
        self.push_toast(
            "Drive queue",
            &format!("queuing {repo} #{issue}{pinned}{after_note}…"),
            ToastSeverity::Info,
        );
    }

    /// `coord drive-queue remove <repo> <issue>`, with the same optimistic
    /// local removal `dispatch_dismiss_escalation` does.
    pub(crate) fn dispatch_drive_queue_remove(&mut self, repo: &str, issue: i64) {
        let issue_str = issue.to_string();
        self.command_runner
            .spawn_queued(&["drive-queue", "remove", repo, &issue_str]);
        self.data
            .drive_queue
            .retain(|e| !(e.repo_name == repo && e.issue_number == issue));
        let len = self.drive_queue_entries().len();
        if self.drive_queue_sel >= len {
            self.drive_queue_sel = len.saturating_sub(1);
        }
        self.push_toast(
            "Drive queue",
            &format!("removed {repo} #{issue} from the queue"),
            ToastSeverity::Info,
        );
    }

    /// `coord drive-queue move <repo> <issue> --to <position>`. Clamped
    /// client-side into `[0, len-1]` so the menu never sends a position the
    /// CLI would have to clamp for us (it does clamp, but a toast that says
    /// "→ 4" when the queue has 3 rows is a lie).
    pub(crate) fn dispatch_drive_queue_move(&mut self, repo: &str, issue: i64, to: i64) {
        let len = self.data.drive_queue.len() as i64;
        let to = to.clamp(0, (len - 1).max(0));
        let issue_str = issue.to_string();
        let to_str = to.to_string();
        self.command_runner
            .spawn_queued(&["drive-queue", "move", repo, &issue_str, "--to", &to_str]);
        self.push_toast(
            "Drive queue",
            &format!("moving {repo} #{issue} to position {to}…"),
            ToastSeverity::Info,
        );
    }

    /// "Unblock" — clear a `blocked` row back to `waiting`.
    ///
    /// There is deliberately **no** `coord drive-queue reset` (DQ-1 keeps run
    /// state out of the operator's write surface), so remove + re-add IS the
    /// reset — a fresh row is `waiting` with `attempts=0` and no `after`.
    /// This mirrors `_requeue_command`'s recipe in
    /// `coord/commands/drive_queue.py` exactly, except that the two halves
    /// are queued as separate `coord` invocations (FIFO, so ordered) rather
    /// than joined by a shell `&&` — `CommandRunner` spawns argv, not a
    /// shell.
    ///
    /// Dropping `after` is intentional and is the *point*: an unsatisfiable
    /// pre-req is one of the two things that blocks a row, and re-adding
    /// with the same `after` would re-block it immediately. The machine pin
    /// is preserved because that is an operator intent, not run state.
    pub(crate) fn dispatch_drive_queue_unblock(&mut self, repo: &str, issue: i64) {
        let machine = self
            .data
            .drive_queue
            .iter()
            .find(|e| e.repo_name == repo && e.issue_number == issue)
            .and_then(|e| e.machine.clone())
            .filter(|m| !m.is_empty());
        let issue_str = issue.to_string();
        self.command_runner
            .spawn_queued(&["drive-queue", "remove", repo, &issue_str]);
        let mut add: Vec<String> = vec![
            "drive-queue".to_string(),
            "add".to_string(),
            repo.to_string(),
            issue_str,
        ];
        if let Some(m) = &machine {
            add.push("--machine".to_string());
            add.push(m.clone());
        }
        let add_refs: Vec<&str> = add.iter().map(|s| s.as_str()).collect();
        self.command_runner.spawn_queued(&add_refs);
        // Optimistic: the row goes back to `waiting` with its counters
        // cleared, which is exactly what the re-add produces.
        for e in self.data.drive_queue.iter_mut() {
            if e.repo_name == repo && e.issue_number == issue {
                e.state = QUEUE_STATE_WAITING.to_string();
                e.attempts = 0;
                e.deferrals = 0;
                e.last_reason = String::new();
                e.after.clear();
            }
        }
        self.push_toast(
            "Drive queue",
            &format!("unblocking {repo} #{issue} (remove + re-add)…"),
            ToastSeverity::Info,
        );
    }

    /// Open the "Add to drive queue after…" prompt for (repo, issue).
    pub(crate) fn open_drive_queue_after_input(&mut self, repo: &str, issue: u64) {
        self.pending_drive_queue_after = Some(PendingDriveQueueAfter {
            repo_name: repo.to_string(),
            issue_number: issue,
            buf: String::new(),
        });
    }

    /// Submit the "after…" prompt: split the buffer on commas/whitespace,
    /// strip a leading `#`, and pass each spec through as its own `--after`.
    ///
    /// Deliberately does NOT validate the specs here — `coord drive-queue
    /// add` already refuses a self-edge or a cycle *before* writing anything
    /// (`validate_enqueue`), and duplicating that rule in Rust would be a
    /// second source of truth that can drift. A `REPO#N` spec is passed
    /// through untouched so a cross-repo pre-req still works.
    pub(crate) fn submit_drive_queue_after_input(&mut self, input: PendingDriveQueueAfter) {
        let after: Vec<String> = input
            .buf
            .split(|c: char| c == ',' || c.is_whitespace())
            .map(|s| s.trim().trim_start_matches('#').to_string())
            .filter(|s| !s.is_empty())
            .collect();
        if after.is_empty() {
            self.push_toast(
                "Drive queue",
                "no pre-req issues given — nothing queued.",
                ToastSeverity::Warning,
            );
            return;
        }
        self.dispatch_drive_queue_add(&input.repo_name, input.issue_number, None, &after);
    }

    /// Keyboard handling while the overlay owns input. Returns `true` when
    /// the key was consumed (which, for this overlay, is every key — it is
    /// modal, same as the fleet-health overlay).
    pub(crate) fn drive_queue_overlay_key(&mut self, key: &Key) -> bool {
        match key {
            Key::Named(NamedKey::Escape) => {
                self.close_drive_queue_overlay();
            }
            Key::Named(NamedKey::Down) | Key::Char('j') => self.drive_queue_move_selection(1),
            Key::Named(NamedKey::Up) | Key::Char('k') => self.drive_queue_move_selection(-1),
            Key::Char('J') => self.drive_queue_selected_move(1),
            Key::Char('K') => self.drive_queue_selected_move(-1),
            Key::Char('x') => {
                if let Some((repo, issue)) = self.drive_queue_selected_key() {
                    self.dispatch_drive_queue_remove(&repo, issue);
                }
            }
            Key::Char('u') => {
                let target = self
                    .drive_queue_entries()
                    .get(self.drive_queue_sel)
                    .filter(|e| e.state == QUEUE_STATE_BLOCKED)
                    .map(|e| (e.repo_name.clone(), e.issue_number));
                match target {
                    Some((repo, issue)) => self.dispatch_drive_queue_unblock(&repo, issue),
                    None => self.push_toast(
                        "Drive queue",
                        "only a blocked entry can be unblocked.",
                        ToastSeverity::Warning,
                    ),
                }
            }
            _ => {}
        }
        true
    }

    /// `(repo, issue)` of the overlay's selected row.
    fn drive_queue_selected_key(&self) -> Option<(String, i64)> {
        self.drive_queue_entries()
            .get(self.drive_queue_sel)
            .map(|e| (e.repo_name.clone(), e.issue_number))
    }

    /// Move the selected row `delta` slots and follow it with the selection
    /// (so repeated `K` walks an entry up the queue instead of walking the
    /// cursor off it).
    pub(crate) fn drive_queue_selected_move(&mut self, delta: i64) {
        let rows = self.drive_queue_entries();
        let len = rows.len();
        let Some(e) = rows.get(self.drive_queue_sel) else {
            return;
        };
        let (repo, issue, position) = (e.repo_name.clone(), e.issue_number, e.position);
        let to = (position + delta).clamp(0, (len as i64 - 1).max(0));
        if to == position {
            return;
        }
        self.dispatch_drive_queue_move(&repo, issue, to);
        self.drive_queue_move_selection(delta as isize);
    }
}

fn dq_muted_row(text: &str) -> ListItem {
    ListItem {
        text: StyledText {
            spans: vec![StyledSpan::with_fg(
                text.to_string(),
                Color::rgb(140, 140, 150),
            )],
        },
        icon: None,
        detail: None,
        decoration: Decoration::Muted,
    }
}

/// Row colour by wire `state`. An unrecognised state renders neutral — never
/// silently green (#1485's "absence must never read as healthy" applied to a
/// state string this build has never heard of).
fn dq_state_colors(state: &str) -> (Color, Color) {
    match state {
        QUEUE_STATE_RUNNING => (Color::rgb(120, 210, 120), Color::rgb(15, 50, 15)),
        QUEUE_STATE_BLOCKED => (Color::rgb(255, 120, 120), Color::rgb(60, 15, 15)),
        QUEUE_STATE_DONE => (Color::rgb(140, 140, 150), Color::rgb(30, 30, 40)),
        QUEUE_STATE_WAITING => (Color::rgb(210, 210, 220), Color::rgb(40, 40, 55)),
        _ => (Color::rgb(210, 210, 220), Color::rgb(40, 40, 55)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::fixtures::make_test_app;

    fn entry(
        issue: i64,
        position: i64,
        state: &str,
        after: &[&str],
    ) -> BoardDriveQueueEntry {
        BoardDriveQueueEntry {
            repo_name: "myrepo".to_string(),
            issue_number: issue,
            position,
            state: state.to_string(),
            after: after.iter().map(|s| s.to_string()).collect(),
            ..BoardDriveQueueEntry::default()
        }
    }

    fn machine(name: &str) -> Machine {
        Machine {
            name: name.to_string(),
            host: String::new(),
            reachable: true,
            active_count: 0,
            repos: vec!["myrepo".to_string()],
            version: None,
            worktree_bytes: 0,
        }
    }

    fn pipeline_issue(number: u64, coord_repo: Option<&str>) -> PipelineIssue {
        PipelineIssue {
            number,
            title: format!("issue {number}"),
            body: String::new(),
            repo_slug: format!("acme/{}", coord_repo.unwrap_or("unmapped")),
            coord_repo: coord_repo.map(str::to_string),
            matched_labels: vec!["coord".to_string()],
            all_labels: vec!["coord".to_string()],
            is_closed: false,
        }
    }

    // ── drive_queue_status_text: the four states ─────────────────────────

    #[test]
    fn status_text_empty_says_empty_not_nothing() {
        assert_eq!(drive_queue_status_text(&[]), "QUEUE: empty");
    }

    #[test]
    fn status_text_normal_counts_running_and_waiting() {
        let rows = vec![
            entry(1, 0, QUEUE_STATE_RUNNING, &[]),
            entry(2, 1, QUEUE_STATE_WAITING, &[]),
            entry(3, 2, QUEUE_STATE_WAITING, &[]),
            entry(4, 3, QUEUE_STATE_WAITING, &[]),
        ];
        assert_eq!(
            drive_queue_status_text(&rows),
            "QUEUE: 1 running · 3 waiting"
        );
        assert_eq!(summarize_drive_queue(&rows).level, DriveQueueLevel::Normal);
    }

    /// Three waiting entries and no eligible one — the epic's alert case.
    /// Each waits on the next, so nothing at the head can start either
    /// (#1 waits on #2 waits on #3 waits on #1: a cycle DQ-1 would refuse to
    /// create, used here purely to make every row ineligible at once).
    #[test]
    fn status_text_stalled_when_nothing_is_eligible() {
        let rows = vec![
            entry(1, 0, QUEUE_STATE_WAITING, &["myrepo#2"]),
            entry(2, 1, QUEUE_STATE_WAITING, &["myrepo#3"]),
            entry(3, 2, QUEUE_STATE_WAITING, &["myrepo#1"]),
        ];
        assert_eq!(
            drive_queue_status_text(&rows),
            "QUEUE: STALLED — 3 waiting, none eligible"
        );
        let s = summarize_drive_queue(&rows);
        assert_eq!(s.level, DriveQueueLevel::Stalled);
        assert_eq!(s.eligible, 0);
    }

    #[test]
    fn stalled_renders_in_warn_colours() {
        let (fg, bg) = DriveQueueLevel::Stalled.colors();
        // Same amber pair `FleetSeverity::Warn` uses — the two adjacent
        // status-bar segments must never disagree about what amber means.
        assert_eq!(fg, Color::rgb(255, 210, 100));
        assert_eq!(bg, Color::rgb(70, 45, 10));
    }

    #[test]
    fn blocked_renders_in_crit_colours() {
        let (fg, bg) = DriveQueueLevel::Blocked.colors();
        assert_eq!(fg, Color::rgb(255, 255, 255));
        assert_eq!(bg, Color::rgb(150, 30, 30));
    }

    /// Blocked outranks stalled: a board that would otherwise read STALLED
    /// must read BLOCKED once any row is blocked.
    #[test]
    fn status_text_blocked_outranks_a_simultaneous_stall() {
        let rows = vec![
            entry(1, 0, QUEUE_STATE_BLOCKED, &[]),
            entry(2, 1, QUEUE_STATE_BLOCKED, &[]),
            // Waiting on a pre-req that is itself still in the queue →
            // ineligible, i.e. this board is ALSO stalled.
            entry(3, 2, QUEUE_STATE_WAITING, &["myrepo#1"]),
        ];
        assert_eq!(
            drive_queue_status_text(&rows),
            "QUEUE: BLOCKED 2 · 1 waiting"
        );
        assert_eq!(summarize_drive_queue(&rows).level, DriveQueueLevel::Blocked);
    }

    /// A queue that is moving is not stalled, even though its tail can't
    /// start yet — that's just a queue.
    #[test]
    fn running_entry_means_waiting_behind_it_is_not_a_stall() {
        let rows = vec![
            entry(1, 0, QUEUE_STATE_RUNNING, &[]),
            entry(2, 1, QUEUE_STATE_WAITING, &["myrepo#1"]),
        ];
        assert_eq!(summarize_drive_queue(&rows).level, DriveQueueLevel::Normal);
        assert_eq!(drive_queue_status_text(&rows), "QUEUE: 1 running · 1 waiting");
    }

    /// `done` rows are history — they must not keep the segment shouting.
    #[test]
    fn done_entries_do_not_count_as_pending() {
        let rows = vec![
            entry(1, 0, QUEUE_STATE_DONE, &[]),
            entry(2, 1, QUEUE_STATE_DONE, &[]),
        ];
        assert_eq!(drive_queue_status_text(&rows), "QUEUE: empty");
    }

    /// A pre-req that already finished satisfies the `after` edge.
    #[test]
    fn a_done_prereq_makes_the_dependent_entry_eligible() {
        let rows = vec![
            entry(1, 0, QUEUE_STATE_DONE, &[]),
            entry(2, 1, QUEUE_STATE_WAITING, &["myrepo#1"]),
        ];
        let s = summarize_drive_queue(&rows);
        assert_eq!(s.eligible, 1);
        assert_eq!(s.level, DriveQueueLevel::Normal);
    }

    /// A pre-req that isn't in the queue at all is treated as satisfied —
    /// it may have landed long before this queue existed. The tick is the
    /// authority; the TUI must not invent a stall it can't substantiate.
    #[test]
    fn a_prereq_absent_from_the_queue_counts_as_satisfied() {
        let rows = vec![entry(2, 0, QUEUE_STATE_WAITING, &["myrepo#999"])];
        assert_eq!(summarize_drive_queue(&rows).eligible, 1);
    }

    /// An unrecognised state from a newer daemon is neither eligible nor a
    /// stall trigger — and never silently folded into a healthy bucket.
    #[test]
    fn unknown_state_neither_triggers_nor_masks_a_stall() {
        let rows = vec![entry(1, 0, "some-future-state", &[])];
        let s = summarize_drive_queue(&rows);
        assert_eq!(s.waiting, 0);
        assert_eq!(s.eligible, 0);
        assert_eq!(s.level, DriveQueueLevel::Empty);
    }

    // ── status-bar segment ───────────────────────────────────────────────

    #[test]
    fn status_bar_segment_is_always_present_even_when_empty() {
        let app = make_test_app(BoardData::default());
        let seg = app.drive_queue_status_bar_segment();
        assert!(
            seg.text.contains("QUEUE: empty"),
            "an empty queue must SAY it's empty: {:?}",
            seg.text
        );
        assert!(seg.action_id.is_none(), "the segment carries no action_id");
        assert!(!seg.bold, "an idle queue must not compete for attention");
    }

    #[test]
    fn status_bar_segment_bolds_only_when_it_is_news() {
        let app = make_test_app(BoardData {
            drive_queue: vec![entry(1, 0, QUEUE_STATE_BLOCKED, &[])],
            ..BoardData::default()
        });
        let seg = app.drive_queue_status_bar_segment();
        assert!(seg.bold, "a blocked queue must be bold");
        assert_eq!(seg.bg, Color::rgb(150, 30, 30));
    }

    // ── menu items ───────────────────────────────────────────────────────

    #[test]
    fn status_bar_menu_offers_drive_queue() {
        let app = make_test_app(BoardData::default());
        let items = app.context_menu_items_for_drive_queue_segment();
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].label, "Drive queue…");
        assert_eq!(items[0].action_id.as_deref(), Some("open-drive-queue-detail"));
        assert_eq!(
            items[0].shortcut.as_deref(),
            Some("q"),
            "the accelerator is shown INSIDE the menu, not as a bar letter row"
        );
    }

    #[test]
    fn pipeline_row_menu_offers_add_with_machine_submenu_and_after() {
        let app = make_test_app(BoardData {
            machines: vec![machine("precision"), machine("dellserver")],
            ..BoardData::default()
        });
        let items = app.drive_queue_menu_items_for_pipeline_row(Some(42), Some("myrepo"), true);
        let labels: Vec<&str> = items.iter().map(|i| i.label.as_str()).collect();
        assert_eq!(
            labels,
            vec![
                "Add to drive queue",
                "Add to drive queue on…",
                "Add to drive queue after…"
            ]
        );
        let submenu = items[1].submenu.as_ref().expect("machine submenu");
        assert_eq!(
            submenu.iter().map(|i| i.label.as_str()).collect::<Vec<_>>(),
            vec!["precision", "dellserver"],
            "the submenu lists the BOARD's machines"
        );
        assert_eq!(
            submenu[0].action_id.as_deref(),
            Some("drive-queue-add-on:precision"),
            "the chosen machine rides in the action id → --machine NAME"
        );
    }

    #[test]
    fn pipeline_row_menu_omits_the_machine_submenu_when_the_board_has_none() {
        let app = make_test_app(BoardData::default());
        let items = app.drive_queue_menu_items_for_pipeline_row(Some(42), Some("myrepo"), true);
        assert!(
            items.iter().all(|i| i.submenu.is_none()),
            "an empty submenu is a dead end, not an affordance"
        );
    }

    #[test]
    fn pipeline_row_menu_offers_remove_for_an_already_queued_row() {
        let app = make_test_app(BoardData {
            drive_queue: vec![entry(42, 3, QUEUE_STATE_WAITING, &[])],
            ..BoardData::default()
        });
        let items = app.drive_queue_menu_items_for_pipeline_row(Some(42), Some("myrepo"), true);
        assert_eq!(items.len(), 1, "Add variants are suppressed: {items:?}");
        assert_eq!(items[0].action_id.as_deref(), Some("drive-queue-row-remove"));
        assert!(
            items[0].label.contains("position 3"),
            "the label names the slot it holds: {:?}",
            items[0].label
        );
    }

    #[test]
    fn pipeline_row_menu_is_empty_for_an_unmapped_repo() {
        let app = make_test_app(BoardData::default());
        assert!(app
            .drive_queue_menu_items_for_pipeline_row(Some(42), None, true)
            .is_empty());
        assert!(app
            .drive_queue_menu_items_for_pipeline_row(None, Some("myrepo"), true)
            .is_empty());
    }

    #[test]
    fn in_progress_row_offers_remove_but_never_a_fresh_add() {
        let queued = make_test_app(BoardData {
            drive_queue: vec![entry(42, 0, QUEUE_STATE_WAITING, &[])],
            ..BoardData::default()
        });
        let items = queued.drive_queue_menu_items_for_pipeline_row(Some(42), Some("myrepo"), false);
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].action_id.as_deref(), Some("drive-queue-row-remove"));

        let unqueued = make_test_app(BoardData::default());
        assert!(
            unqueued
                .drive_queue_menu_items_for_pipeline_row(Some(42), Some("myrepo"), false)
                .is_empty(),
            "an in-progress row that is NOT queued offers nothing"
        );
    }

    #[test]
    fn done_rows_do_not_count_as_queued_for_the_menu_swap() {
        let app = make_test_app(BoardData {
            drive_queue: vec![entry(42, 0, QUEUE_STATE_DONE, &[])],
            ..BoardData::default()
        });
        assert!(!app.drive_queue_contains("myrepo", 42));
        let items = app.drive_queue_menu_items_for_pipeline_row(Some(42), Some("myrepo"), true);
        assert_eq!(items[0].action_id.as_deref(), Some("drive-queue-add"));
    }

    #[test]
    fn overlay_row_menu_gates_moves_at_the_ends_and_unblock_off_waiting() {
        let app = make_test_app(BoardData::default());
        let first = app.context_menu_items_for_drive_queue_row(QUEUE_STATE_WAITING, 0, 3);
        assert!(first[0].disabled, "'Move up' disabled at position 0");
        assert_eq!(first[0].disabled_reason.as_deref(), Some("already first"));
        assert!(!first[1].disabled, "'Move down' enabled mid-queue");
        assert!(
            !first.iter().any(|i| i.label == "Unblock"),
            "a waiting row has nothing to unblock"
        );

        let last = app.context_menu_items_for_drive_queue_row(QUEUE_STATE_BLOCKED, 2, 3);
        assert!(last[1].disabled, "'Move down' disabled at the tail");
        assert!(last.iter().any(|i| i.label == "Unblock"));
    }

    // ── dispatch: the `coord drive-queue …` argv ─────────────────────────

    #[test]
    fn dispatch_add_spawns_a_bare_add() {
        let mut app = make_test_app(BoardData::default());
        app.dispatch_drive_queue_add("myrepo", 42, None, &[]);
        assert_eq!(
            app.command_runner.spawned_calls,
            vec![vec![
                "drive-queue".to_string(),
                "add".to_string(),
                "myrepo".to_string(),
                "42".to_string(),
            ]]
        );
        assert!(
            app.drive_queue_contains("myrepo", 42),
            "optimistically queued so the menu/segment update before the next poll"
        );
    }

    #[test]
    fn dispatch_add_passes_the_chosen_machine_as_a_flag() {
        let mut app = make_test_app(BoardData::default());
        app.dispatch_drive_queue_add("myrepo", 42, Some("dellserver"), &[]);
        assert_eq!(
            app.command_runner.spawned_calls[0],
            vec!["drive-queue", "add", "myrepo", "42", "--machine", "dellserver"]
        );
    }

    #[test]
    fn dispatch_add_repeats_after_once_per_prereq() {
        let mut app = make_test_app(BoardData::default());
        app.dispatch_drive_queue_add(
            "myrepo",
            42,
            None,
            &["1753".to_string(), "other#7".to_string()],
        );
        assert_eq!(
            app.command_runner.spawned_calls[0],
            vec![
                "drive-queue", "add", "myrepo", "42", "--after", "1753", "--after", "other#7"
            ]
        );
    }

    #[test]
    fn dispatch_remove_spawns_and_removes_optimistically() {
        let mut app = make_test_app(BoardData {
            drive_queue: vec![
                entry(42, 0, QUEUE_STATE_WAITING, &[]),
                entry(43, 1, QUEUE_STATE_WAITING, &[]),
            ],
            ..BoardData::default()
        });
        app.dispatch_drive_queue_remove("myrepo", 42);
        assert_eq!(
            app.command_runner.spawned_calls,
            vec![vec!["drive-queue", "remove", "myrepo", "42"]]
        );
        assert_eq!(app.data.drive_queue.len(), 1);
        assert_eq!(app.data.drive_queue[0].issue_number, 43);
    }

    #[test]
    fn dispatch_move_clamps_into_range() {
        let mut app = make_test_app(BoardData {
            drive_queue: vec![
                entry(42, 0, QUEUE_STATE_WAITING, &[]),
                entry(43, 1, QUEUE_STATE_WAITING, &[]),
            ],
            ..BoardData::default()
        });
        app.dispatch_drive_queue_move("myrepo", 42, 99);
        assert_eq!(
            app.command_runner.spawned_calls[0],
            vec!["drive-queue", "move", "myrepo", "42", "--to", "1"],
            "a position past the tail is clamped, not sent verbatim"
        );
    }

    /// There is deliberately no `coord drive-queue reset` — remove + re-add
    /// IS the reset (DQ-1 keeps run state out of the write surface).
    #[test]
    fn dispatch_unblock_is_remove_then_readd_preserving_the_machine_pin() {
        let mut blocked = entry(42, 0, QUEUE_STATE_BLOCKED, &["myrepo#41"]);
        blocked.machine = Some("dellserver".to_string());
        blocked.deferrals = 4;
        blocked.last_reason = "pre-req never landed".to_string();
        let mut app = make_test_app(BoardData {
            drive_queue: vec![blocked],
            ..BoardData::default()
        });
        app.dispatch_drive_queue_unblock("myrepo", 42);
        assert_eq!(
            app.command_runner.spawned_calls,
            vec![vec!["drive-queue", "remove", "myrepo", "42"]],
            "the remove starts immediately"
        );
        assert_eq!(
            app.command_runner.queue_depth(),
            1,
            "the re-add is FIFO-queued behind it, not joined by a shell `&&` \
             (CommandRunner spawns argv, not a shell)"
        );
        // Draining the first command starts the queued second one — the same
        // thing the app's per-tick `poll()` does.
        app.command_runner.poll().expect("no_spawn resolves synchronously");
        assert_eq!(
            app.command_runner.spawned_calls,
            vec![
                vec!["drive-queue", "remove", "myrepo", "42"],
                vec!["drive-queue", "add", "myrepo", "42", "--machine", "dellserver"],
            ],
            "…and the re-add preserves the machine pin (an operator intent, \
             not run state)"
        );
        let row = &app.data.drive_queue[0];
        assert_eq!(row.state, QUEUE_STATE_WAITING);
        assert_eq!(row.deferrals, 0);
        assert!(row.last_reason.is_empty());
        assert!(
            row.after.is_empty(),
            "the unsatisfiable pre-req is dropped — re-adding it would re-block immediately"
        );
    }

    // ── after… prompt ────────────────────────────────────────────────────

    #[test]
    fn after_input_splits_on_commas_and_whitespace_and_strips_hashes() {
        let mut app = make_test_app(BoardData::default());
        app.submit_drive_queue_after_input(PendingDriveQueueAfter {
            repo_name: "myrepo".to_string(),
            issue_number: 42,
            buf: "#1753, 1754  other#7".to_string(),
        });
        assert_eq!(
            app.command_runner.spawned_calls[0],
            vec![
                "drive-queue", "add", "myrepo", "42", "--after", "1753", "--after", "1754",
                "--after", "other#7"
            ]
        );
    }

    #[test]
    fn after_input_with_no_prereqs_queues_nothing() {
        let mut app = make_test_app(BoardData::default());
        app.submit_drive_queue_after_input(PendingDriveQueueAfter {
            repo_name: "myrepo".to_string(),
            issue_number: 42,
            buf: "   ".to_string(),
        });
        assert!(
            app.command_runner.spawned_calls.is_empty(),
            "an empty after-list must not silently become a bare add"
        );
    }

    // ── overlay state ────────────────────────────────────────────────────

    #[test]
    fn overlay_entries_are_returned_in_position_order() {
        let app = make_test_app(BoardData {
            drive_queue: vec![
                entry(9, 2, QUEUE_STATE_WAITING, &[]),
                entry(7, 0, QUEUE_STATE_RUNNING, &[]),
                entry(8, 1, QUEUE_STATE_WAITING, &[]),
            ],
            ..BoardData::default()
        });
        assert_eq!(
            app.drive_queue_entries()
                .iter()
                .map(|e| e.issue_number)
                .collect::<Vec<_>>(),
            vec![7, 8, 9]
        );
    }

    #[test]
    fn overlay_selection_clamps_and_never_wraps() {
        let mut app = make_test_app(BoardData {
            drive_queue: vec![
                entry(1, 0, QUEUE_STATE_WAITING, &[]),
                entry(2, 1, QUEUE_STATE_WAITING, &[]),
            ],
            ..BoardData::default()
        });
        app.drive_queue_move_selection(-1);
        assert_eq!(app.drive_queue_sel, 0, "no wrap past the head");
        app.drive_queue_move_selection(5);
        assert_eq!(app.drive_queue_sel, 1, "no wrap past the tail");
    }

    #[test]
    fn opening_the_overlay_clamps_a_stale_selection() {
        let mut app = make_test_app(BoardData {
            drive_queue: vec![entry(1, 0, QUEUE_STATE_WAITING, &[])],
            ..BoardData::default()
        });
        app.drive_queue_sel = 9;
        app.open_drive_queue_overlay();
        assert_eq!(app.drive_queue_sel, 0);
    }

    #[test]
    fn overlay_key_x_removes_the_selected_row() {
        let mut app = make_test_app(BoardData {
            drive_queue: vec![
                entry(1, 0, QUEUE_STATE_WAITING, &[]),
                entry(2, 1, QUEUE_STATE_WAITING, &[]),
            ],
            ..BoardData::default()
        });
        app.open_drive_queue_overlay();
        app.drive_queue_overlay_key(&Key::Char('j'));
        app.drive_queue_overlay_key(&Key::Char('x'));
        assert_eq!(
            app.command_runner.spawned_calls[0],
            vec!["drive-queue", "remove", "myrepo", "2"]
        );
    }

    #[test]
    fn overlay_key_u_refuses_a_row_that_is_not_blocked() {
        let mut app = make_test_app(BoardData {
            drive_queue: vec![entry(1, 0, QUEUE_STATE_WAITING, &[])],
            ..BoardData::default()
        });
        app.open_drive_queue_overlay();
        app.drive_queue_overlay_key(&Key::Char('u'));
        assert!(
            app.command_runner.spawned_calls.is_empty(),
            "unblocking a waiting row must not shell anything"
        );
    }

    #[test]
    fn overlay_key_shift_k_moves_the_entry_and_follows_it() {
        let mut app = make_test_app(BoardData {
            drive_queue: vec![
                entry(1, 0, QUEUE_STATE_WAITING, &[]),
                entry(2, 1, QUEUE_STATE_WAITING, &[]),
            ],
            ..BoardData::default()
        });
        app.open_drive_queue_overlay();
        app.drive_queue_sel = 1;
        app.drive_queue_overlay_key(&Key::Char('K'));
        assert_eq!(
            app.command_runner.spawned_calls[0],
            vec!["drive-queue", "move", "myrepo", "2", "--to", "0"]
        );
        assert_eq!(app.drive_queue_sel, 0, "selection follows the moved entry");
    }

    #[test]
    fn overlay_escape_closes() {
        let mut app = make_test_app(BoardData::default());
        app.open_drive_queue_overlay();
        app.drive_queue_overlay_key(&Key::Named(NamedKey::Escape));
        assert!(!app.drive_queue_overlay_open);
    }

    #[test]
    fn overlay_context_target_carries_the_selected_row() {
        let mut app = make_test_app(BoardData {
            drive_queue: vec![
                entry(1, 0, QUEUE_STATE_WAITING, &[]),
                entry(2, 1, QUEUE_STATE_BLOCKED, &[]),
            ],
            ..BoardData::default()
        });
        app.open_drive_queue_overlay();
        app.drive_queue_sel = 1;
        match app.drive_queue_context_target().expect("a target") {
            ContextMenuTarget::DriveQueueRow {
                repo_name,
                issue_number,
                state,
                position,
                queue_len,
            } => {
                assert_eq!(repo_name, "myrepo");
                assert_eq!(issue_number, 2);
                assert_eq!(state, QUEUE_STATE_BLOCKED);
                assert_eq!(position, 1);
                assert_eq!(queue_len, 2);
            }
            other => panic!("wrong target: {other:?}"),
        }
    }

    #[test]
    fn overlay_context_target_is_none_for_an_empty_queue() {
        let mut app = make_test_app(BoardData::default());
        app.open_drive_queue_overlay();
        assert!(app.drive_queue_context_target().is_none());
    }

    // ── TuiDriver black-box (#1755 acceptance) ───────────────────────────

    /// Build a `CoordApp` whose Pipeline view is populated and whose queue is
    /// `rows`, ready to hand to `driver_with_shell`.
    fn driver_app(rows: Vec<BoardDriveQueueEntry>) -> CoordApp {
        let mut app = make_test_app(BoardData {
            drive_queue: rows,
            machines: vec![machine("precision"), machine("dellserver")],
            ..BoardData::default()
        });
        app.pipeline_issues = vec![pipeline_issue(42, Some("myrepo"))];
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        app.rebuild_pipeline_sidebar(None);
        app
    }

    /// The status bar renders the `QUEUE:` segment on first paint, in every
    /// one of the four states — the issue's headline acceptance bullets,
    /// asserted through a real render rather than the pure function alone.
    #[test]
    fn tuidriver_status_bar_renders_every_queue_state() {
        use quadraui::tui::testing::driver_with_shell;

        let cases: Vec<(Vec<BoardDriveQueueEntry>, &str)> = vec![
            (Vec::new(), "QUEUE: empty"),
            (
                vec![
                    entry(1, 0, QUEUE_STATE_RUNNING, &[]),
                    entry(2, 1, QUEUE_STATE_WAITING, &[]),
                    entry(3, 2, QUEUE_STATE_WAITING, &[]),
                    entry(4, 3, QUEUE_STATE_WAITING, &[]),
                ],
                "QUEUE: 1 running · 3 waiting",
            ),
            (
                vec![
                    entry(1, 0, QUEUE_STATE_WAITING, &["myrepo#2"]),
                    entry(2, 1, QUEUE_STATE_WAITING, &["myrepo#3"]),
                    entry(3, 2, QUEUE_STATE_WAITING, &["myrepo#1"]),
                ],
                "QUEUE: STALLED — 3 waiting, none eligible",
            ),
            (
                vec![
                    entry(1, 0, QUEUE_STATE_BLOCKED, &[]),
                    entry(2, 1, QUEUE_STATE_BLOCKED, &[]),
                    entry(3, 2, QUEUE_STATE_WAITING, &["myrepo#1"]),
                ],
                "QUEUE: BLOCKED 2 · 1 waiting",
            ),
        ];
        for (rows, expected) in cases {
            let driver = driver_with_shell(driver_app(rows), CoordApp::shell_config(), 200, 40);
            let screen = driver.screen();
            assert!(
                screen.contains(expected),
                "status bar must render {expected:?}:\n{screen}"
            );
        }
    }

    /// "The status bar ALWAYS shows a `QUEUE:` segment" has to hold at a
    /// real terminal width, not just a 200-column test canvas: the bar drops
    /// whole trailing `left` segments once the view hints claim the row, and
    /// appended *after* "FLEET: OK  (coord health for detail)" the QUEUE
    /// segment vanished outright below ~150 columns. Ordered ahead of it,
    /// both survive — and in the common (empty/normal) case fleet health
    /// keeps its full text too. See `status_bar()`'s ordering comment.
    #[test]
    fn tuidriver_queue_segment_survives_a_narrow_terminal() {
        use quadraui::tui::testing::driver_with_shell;

        for width in [140u16, 160, 200] {
            let driver = driver_with_shell(
                driver_app(Vec::new()),
                CoordApp::shell_config(),
                width,
                40,
            );
            let screen = driver.screen();
            assert!(
                screen.contains("QUEUE: empty"),
                "the QUEUE segment must survive a {width}-column terminal:\n{screen}"
            );
            assert!(
                screen.contains("FLEET:"),
                "…without displacing the #1631 fleet-health verdict:\n{screen}"
            );
        }

        // At 120 columns the bar is over-subscribed no matter what: even
        // BEFORE #1755 the fleet segment rendered clipped there ("FLEET: OK
        // (coord"). The rule that must not bend is the newer, actionable
        // one — the QUEUE verdict stays whole, and fleet health loses more
        // of its trailing "(coord health for detail)" CLI hint.
        let driver =
            driver_with_shell(driver_app(Vec::new()), CoordApp::shell_config(), 120, 40);
        let screen = driver.screen();
        assert!(
            screen.contains("QUEUE: empty"),
            "even at 120 columns the QUEUE verdict must be whole:\n{screen}"
        );
    }

    /// Right-click the status bar → "Drive queue…" → the overlay lists every
    /// entry in `position` order, with `deferrals` / `last_reason` for the
    /// entry being skipped.
    #[test]
    fn tuidriver_status_bar_menu_opens_the_overlay_in_position_order() {
        use quadraui::tui::testing::driver_with_shell;

        let mut skipped = entry(9, 1, QUEUE_STATE_WAITING, &["myrepo#7"]);
        skipped.deferrals = 3;
        skipped.last_reason = "pre-req myrepo#7 has not merged".to_string();
        let app = driver_app(vec![entry(7, 0, QUEUE_STATE_RUNNING, &[]), skipped]);
        let mut driver = driver_with_shell(app, CoordApp::shell_config(), 200, 44);

        let (x, y) = driver
            .find("QUEUE:")
            .unwrap_or_else(|| panic!("QUEUE segment not on screen:\n{}", driver.screen()));
        driver.dispatch(UiEvent::MouseDown {
            widget: None,
            button: MouseButton::Right,
            position: Point::new(x, y),
            modifiers: Modifiers::default(),
        });

        let menu = driver.screen();
        assert!(
            menu.contains("Drive queue…"),
            "right-clicking the status bar must offer 'Drive queue…':\n{menu}"
        );
        assert!(
            menu.contains("Fleet health…"),
            "…without displacing the fleet-health item that was already there:\n{menu}"
        );

        let (mx, my) = driver
            .find("Drive queue…")
            .unwrap_or_else(|| panic!("'Drive queue…' item not found:\n{menu}"));
        // Same fractional-anchor nudge `escalation.rs`'s TuiDriver test
        // documents — `find` returns the row centre, but the menu hit-tests
        // one item-height below where it visibly renders.
        driver.click(mx, my - 0.1);

        let overlay = driver.screen();
        assert!(
            overlay.contains("Drive queue"),
            "the overlay must be open:\n{overlay}"
        );
        let running_at = overlay
            .find("myrepo#7")
            .unwrap_or_else(|| panic!("position-0 entry missing:\n{overlay}"));
        let waiting_at = overlay
            .find("myrepo#9")
            .unwrap_or_else(|| panic!("position-1 entry missing:\n{overlay}"));
        assert!(
            running_at < waiting_at,
            "entries must be listed in position order:\n{overlay}"
        );
        assert!(
            overlay.contains("deferrals 3"),
            "a skipped entry must show its deferral count:\n{overlay}"
        );
        assert!(
            overlay.contains("pre-req myrepo#7 has not merged"),
            "a skipped entry must show WHY it is being passed over:\n{overlay}"
        );
    }

    /// Right-clicking a Pipeline row offers "Add to drive queue"; for a row
    /// already queued it offers "Remove from drive queue" instead.
    #[test]
    fn tuidriver_pipeline_row_menu_offers_add_then_remove() {
        use quadraui::tui::testing::driver_with_shell;

        fn open_row_menu(app: CoordApp) -> String {
            let mut driver = driver_with_shell(app, CoordApp::shell_config(), 200, 44);
            // The lone fixture issue has no milestone, so its row starts
            // collapsed under the "No milestone" bucket (same expansion
            // drive.rs/escalation.rs's TuiDriver tests need).
            let (label_x, label_y) = driver.find("No milestone").unwrap_or_else(|| {
                panic!("'No milestone' bucket header not found:\n{}", driver.screen())
            });
            driver.click((label_x - 2.0).max(0.0), label_y);
            let (x, y) = driver
                .find("#42")
                .unwrap_or_else(|| panic!("Pipeline row '#42' not found:\n{}", driver.screen()));
            driver.dispatch(UiEvent::MouseDown {
                widget: None,
                button: MouseButton::Right,
                position: Point::new(x, y),
                modifiers: Modifiers::default(),
            });
            driver.screen()
        }

        let unqueued = open_row_menu(driver_app(Vec::new()));
        assert!(
            unqueued.contains("Add to drive queue"),
            "an unqueued Pipeline row must offer 'Add to drive queue':\n{unqueued}"
        );
        assert!(
            !unqueued.contains("Remove from drive queue"),
            "…and not the Remove variant:\n{unqueued}"
        );

        let queued = open_row_menu(driver_app(vec![entry(42, 0, QUEUE_STATE_WAITING, &[])]));
        assert!(
            queued.contains("Remove from drive queue"),
            "a queued row must offer 'Remove from drive queue' instead:\n{queued}"
        );
        assert!(
            !queued.contains("Add to drive queue"),
            "…and NOT the Add variants:\n{queued}"
        );
    }
}
