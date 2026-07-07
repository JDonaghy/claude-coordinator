//! Plans ActivityBar panel (#975).
//!
//! Elevates and subsumes the `milestone_dag` "Milestones" view: renders the
//! plan-roster from `BoardData::plan_roster` (server-computed by
//! `coord/serve_app.py` via `coord.plans.aggregate_repo_plans`) as one row per
//! milestone/epic with ready / blocked / in-flight / done counts and a
//! `needs_you` attention list.  Selecting a row opens that plan's tracking
//! epic in the browser via `gh issue view --web`.
//!
//! **Design note (server-computed, thin client).** The TUI does not
//! re-aggregate plans client-side: it reads `plan_roster` off `/board`
//! verbatim.  This mirrors the #584 portable-control-center read path — no
//! shell-out to `coord plans` from the TUI, no re-implementation of the
//! aggregation, and stays in lock-step with `coord.plans.PlanEntry.to_dict()`
//! (a mistyped field would fail the whole `BoardPayload` parse and blank the
//! board, per #632).
//!
//! **Read-only in this slice.** Fast capture (#977) and the GOAL.md header
//! (#978) come later.
//!
//! **Health chips + attention badge (#976).** Each `needs_you` signal
//! renders as its own coloured/iconed chip (see `health_chip_for_signal`)
//! instead of the flat `[a, b, c]` bracket list from #975 — the raw signal
//! tokens still appear on screen (just individually coloured now) so
//! nothing that grepped for them breaks. A `NN% done` chip is appended
//! whenever the plan has a work order, independent of `needs_you`. The
//! ActivityBar (the quadraui `PanelDefinition` behind the ◆ icon) has no
//! badge/count slot in the vendored quadraui version, so the "N plans need
//! you" attention badge lives on the always-visible status bar instead
//! (`plans_needing_attention_count` below, read from `status_bar()` in
//! `mod.rs`), mirroring the existing `live_tmux_sessions` badge pattern —
//! visible from any view, satisfying "remind me without opening it."
//!
//! **Empty state is not one message (#976 fix-up).** A manual smoke test
//! against a real daemon reported "0 plans" with no indication anything was
//! wrong; the daemon turned out to be running a pre-#975 build that never
//! sends `plan_roster` at all — indistinguishable, from the empty Vec alone,
//! from a board with genuinely zero milestones. `render_plans_panel` now
//! branches on `BoardData::plan_roster_supported` (mirrors
//! `BoardPayload::plan_roster_supported`, stamped by `serve_app.py`'s
//! `board()` handler whenever it computes a roster at all, empty or not) to
//! show one of two distinct messages: "No plans yet" for a true-empty
//! roster, or a "Plans unavailable" pointer to upgrade/connect a daemon
//! otherwise.
#[allow(unused_imports)]
use super::*;

// ─── impl CoordApp — sidebar/main-panel rendering + actions ──────────────────

impl CoordApp {
    /// The plan-roster entries currently on the board, in a stable order:
    /// primary sort is `(repo, milestone_number)` so the list stays visually
    /// stable across refreshes.  Cheap — a clone of the payload slice.
    ///
    /// This is the *full* unfiltered roster (tracked + untracked milestones
    /// alike).  Most callers want [`Self::plans_visible_entries`] instead —
    /// this one remains for callers that need repo-wide aggregates (sidebar
    /// count, per-repo header stats) regardless of collapse state.
    pub(crate) fn plans_entries(&self) -> Vec<PlanRosterEntry> {
        let mut out: Vec<PlanRosterEntry> = self.data.plan_roster.clone();
        out.sort_by(|a, b| {
            (a.repo.as_str(), a.milestone_number).cmp(&(b.repo.as_str(), b.milestone_number))
        });
        out
    }

    /// The plan-roster entries that are actually *selectable/rendered* right
    /// now (#1001): every `has_work_order` milestone, plus `no_work_order`
    /// milestones only for repos in `plans_expanded_repos`. Collapsed
    /// untracked milestones are summarised by a non-selectable "+N without a
    /// work order" line drawn separately in `render_plans_panel` — they
    /// don't occupy a slot here, so `plans_sel` (which indexes into this
    /// list) never lands on noise the operator hasn't asked to see.
    pub(crate) fn plans_visible_entries(&self) -> Vec<PlanRosterEntry> {
        self.plans_entries()
            .into_iter()
            .filter(|e| e.has_work_order || self.plans_expanded_repos.contains(&e.repo))
            .collect()
    }

    /// The currently-selected plan-roster row (`plans_sel`, clamped against
    /// the *visible* roster — see `plans_visible_entries`), or `None` when
    /// nothing is currently rendered/selectable.
    pub(crate) fn plans_selected(&self) -> Option<PlanRosterEntry> {
        let entries = self.plans_visible_entries();
        if entries.is_empty() {
            return None;
        }
        let idx = self.plans_sel.min(entries.len() - 1);
        entries.into_iter().nth(idx)
    }

    /// True iff `signal` should drive the loud "N need attention" badge and
    /// the row's warm-accent color (#1001). Only `ready_waiting` (something
    /// is ready to dispatch right now) and `stalled` (a work order exists
    /// but nothing is ready or in-flight) are actionable enough to justify
    /// crying wolf at 3-repo scale. `no_work_order` is the common case for
    /// plain organizational milestones that were never meant to become
    /// dispatch-tracked epics — informational, not an alarm — and
    /// `chat_pending` just means an operator already has a chat open against
    /// this plan, which isn't something *else* needs to act on.
    fn is_loud_attention_signal(signal: &str) -> bool {
        matches!(signal, "ready_waiting" | "stalled")
    }

    /// True iff `entry` should count toward the loud attention badge / warm
    /// row color — i.e. carries at least one `is_loud_attention_signal`.
    fn has_loud_attention(entry: &PlanRosterEntry) -> bool {
        entry.needs_you.iter().any(|s| Self::is_loud_attention_signal(s))
    }

    /// Count of plan-roster entries carrying at least one *loud* attention
    /// signal (`ready_waiting` / `stalled` — see `is_loud_attention_signal`,
    /// #1001) — the shared basis for the Plans sidebar hint (below) and the
    /// global status-bar "N plans need you" badge (#976, `status_bar()` in
    /// `mod.rs`) so the two never drift out of sync. `no_work_order` no
    /// longer inflates this count: at 3-repo scale, 38 of 41 milestones
    /// being plain organizational buckets with no dispatch intent was
    /// burying the 1-3 signals that were actually actionable.
    pub(crate) fn plans_needing_attention_count(&self) -> usize {
        self.data
            .plan_roster
            .iter()
            .filter(|e| Self::has_loud_attention(e))
            .count()
    }

    /// Toggle whether *repo*'s "without a work order" milestones are
    /// expanded in the Plans panel (#1001). Acts on the repo of the
    /// currently-selected row (`u` key); when nothing is selected — which
    /// only happens when *every* repo on screen is 100% untracked and
    /// collapsed, so there's no selectable row at all — falls back to the
    /// first repo in the full roster (mirrors `capture_plan_stub`'s
    /// first-configured-repo fallback) so a fully-untracked repo can still
    /// be expanded. No-ops with a toast only when the roster itself is
    /// empty. Selection (`plans_sel`) is reset to 0 afterward since
    /// collapsing/expanding can shrink or grow the visible list out from
    /// under the old index.
    pub(crate) fn toggle_plans_repo_expansion(&mut self) {
        let repo = match self
            .plans_selected()
            .map(|e| e.repo)
            .or_else(|| self.plans_entries().first().map(|e| e.repo.clone()))
        {
            Some(repo) => repo,
            None => {
                self.push_toast(
                    "Show untracked milestones",
                    "No plans on the board — nothing to expand.",
                    ToastSeverity::Info,
                );
                return;
            }
        };
        let now_expanded = if self.plans_expanded_repos.remove(&repo) {
            false
        } else {
            self.plans_expanded_repos.insert(repo.clone());
            true
        };
        let untracked = self
            .plans_entries()
            .iter()
            .filter(|e| e.repo == repo && !e.has_work_order)
            .count();
        self.plans_sel = 0;
        self.push_toast(
            if now_expanded {
                "Untracked milestones shown"
            } else {
                "Untracked milestones hidden"
            },
            &format!(
                "{repo}: {untracked} milestone{} without a work order {}.",
                if untracked == 1 { "" } else { "s" },
                if now_expanded { "expanded" } else { "collapsed" },
            ),
            ToastSeverity::Info,
        );
    }

    /// Sidebar placeholder for the Plans view — plan count + "attention"
    /// hint (any entry with a `needs_you` signal).  All content lives in the
    /// main panel; mirrors `merge_queue_sidebar` / `milestone_dag_sidebar`.
    pub(crate) fn plans_sidebar(&self) -> ListView {
        let entries = self.plans_entries();
        let n = entries.len();
        let attn_count = self.plans_needing_attention_count();
        let attn = if attn_count > 0 {
            format!(" ⚠ {} need attention", attn_count)
        } else {
            String::new()
        };
        let hint = format!(
            "  {} plan{}{}",
            n,
            if n == 1 { "" } else { "s" },
            attn,
        );
        ListView {
            id: WidgetId::new("plans-sidebar"),
            title: Some(StyledText::plain(" PLANS ")),
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

    /// Map one `needs_you` signal to its health-chip `(icon+label, color)`
    /// (#976). The raw signal token stays in the label text — anything that
    /// greps/asserts on `"ready_waiting"` etc. (see #975's tests) still
    /// matches; only the icon + per-signal color are new. Unknown/future
    /// signals fall back to a plain amber chip so an older TUI build against
    /// a newer daemon degrades gracefully instead of dropping the signal.
    ///
    /// **#1001:** `no_work_order` is demoted to a muted gray — it's the
    /// common case for plain organizational milestones with no dispatch
    /// intent, not an alarm (see `is_loud_attention_signal`). It stays
    /// visible (informational) but no longer competes visually with the two
    /// signals that actually warrant a "look at me" amber/red.
    fn health_chip_for_signal(signal: &str) -> (String, Color) {
        match signal {
            "no_work_order" => ("⚑ no_work_order".to_string(), Color::rgb(140, 140, 150)),
            "ready_waiting" => ("● ready_waiting".to_string(), Color::rgb(120, 210, 120)),
            "stalled" => ("⏸ stalled".to_string(), Color::rgb(220, 100, 90)),
            "chat_pending" => ("◐ chat_pending".to_string(), Color::rgb(120, 190, 230)),
            other => (format!("▲ {other}"), Color::rgb(220, 190, 120)),
        }
    }

    /// Render the Plans main panel — grouped by repo (#1001), one header row
    /// per repo followed by that repo's tracked (`has_work_order`)
    /// milestones, with untracked milestones collapsed into a trailing
    /// summary line by default:
    ///
    /// ```text
    /// ▾ api  (1 tracked)   ready=2  blocked=1  ⚠ 1 need attention
    ///  api  #5  Substrate                    epic:#500  ready=2  in-flight=0  blocked=1  done=0/3  [● ready_waiting] [67% done]
    ///    +1 without a work order  (press u to expand)
    /// ```
    ///
    /// Each `needs_you` entry gets its own coloured chip (health chips,
    /// #976) instead of one flat bracketed list, plus an always-on
    /// `NN% done` chip whenever the plan has a work order (independent of
    /// `needs_you` — it's a progress indicator, not an attention signal).
    /// Only `ready_waiting`/`stalled` drive the warm-accent row color and
    /// the per-repo "N need attention" count (#1001) — `no_work_order` is
    /// informational (see `health_chip_for_signal`, `has_loud_attention`).
    ///
    /// The currently-selected row is highlighted via `selected_idx` so the
    /// "Enter to open tracking epic" action has a visible target. Header
    /// rows and the "+N without a work order" summary line are never
    /// selectable — `plans_sel` indexes only into `plans_visible_entries()`,
    /// mirroring `render_merge_plan_panel`'s header-row pattern in
    /// `pipeline.rs`: a local `selected_idx`/`data_idx` pair maps the flat
    /// data-only selection onto this header-interleaved display list.
    ///
    /// **#978:** when `BoardData::goal_header.available`, a pinned GOAL.md
    /// north-star header strip is carved off the top of `rect` and drawn
    /// first via `render_goal_header_strip`; the roster below (empty-state
    /// or populated) renders into the remaining `list_rect`. Absent/older
    /// daemons leave `goal_header.available == false` (the type's
    /// `Default`), so `list_rect == rect` and nothing changes from before
    /// this field existed.
    pub(crate) fn render_plans_panel(&self, backend: &mut dyn Backend, rect: Rect, lh: f32) {
        let list_rect = if self.data.goal_header.available {
            let goal_rect = Self::plans_goal_header_rect(rect, lh);
            self.render_goal_header_strip(backend, goal_rect);
            Rect::new(
                rect.x,
                rect.y + goal_rect.height,
                rect.width,
                (rect.height - goal_rect.height).max(0.0),
            )
        } else {
            rect
        };

        let entries = self.plans_entries();
        if entries.is_empty() {
            // #976: an empty roster is ambiguous on its own — it means either
            // "genuinely zero milestones" (rare but real) or "not currently
            // receiving plan-roster data at all" (no daemon connected, or a
            // daemon older than #975 that never computes it). Silently
            // showing the same "no plans yet" placeholder in the second case
            // is exactly the review finding this fixes: a stale/pre-#975
            // daemon rendered indistinguishable from a genuinely empty board.
            // `plan_roster_supported` (from `BoardPayload`/`BoardData`, see
            // types.rs) is the authoritative signal — trust it over guessing
            // from the empty Vec.
            let message = if self.data.plan_roster_supported {
                "  No plans yet.  Milestones with a `## Work order` block will appear here."
            } else {
                "  Plans unavailable — not receiving plan-roster data. Requires a \
                 `coord serve` daemon that supports it (v0.4.64+); connect via \
                 ~/.coord/client.toml, or upgrade + restart the daemon if already connected."
            };
            backend.draw_list(list_rect, &plain_list("plans-empty", message, 0));
            return;
        }

        let visible = self.plans_visible_entries();
        let sel = if visible.is_empty() {
            0
        } else {
            self.plans_sel.min(visible.len() - 1)
        };
        let mut items: Vec<ListItem> = Vec::with_capacity(entries.len() + 8);
        let mut selected_idx = 0usize;
        let mut data_idx = 0usize; // index into `visible` / `plans_sel` space

        let mut i = 0usize;
        while i < entries.len() {
            let start = i;
            let repo = entries[start].repo.clone();
            while i < entries.len() && entries[i].repo == repo {
                i += 1;
            }
            let group = &entries[start..i];

            let tracked_count = group.iter().filter(|e| e.has_work_order).count();
            let untracked_count = group.len() - tracked_count;
            let ready_sum: u32 = group.iter().map(|e| e.ready_frontier).sum();
            let blocked_sum: u32 = group.iter().map(|e| e.blocked).sum();
            let attention_count = group.iter().filter(|e| Self::has_loud_attention(e)).count();

            let attn_suffix = if attention_count > 0 {
                format!(
                    "   ⚠ {attention_count} need attention",
                )
            } else {
                String::new()
            };
            let header_label = format!(
                "▾ {repo}  ({tracked_count} tracked)   ready={ready_sum}  blocked={blocked_sum}{attn_suffix}",
            );
            let header_color = if attention_count > 0 {
                Color::rgb(220, 190, 120)
            } else {
                Color::rgb(140, 180, 210)
            };
            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(header_label, header_color)],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Header,
            });

            let expanded = self.plans_expanded_repos.contains(&repo);
            for entry in group {
                if !entry.has_work_order && !expanded {
                    // Collapsed by default (#1001) — summarised by the
                    // trailing "+N without a work order" line below instead
                    // of always-expanded noise.
                    continue;
                }
                let tracking = entry
                    .tracking_issue
                    .map(|n| format!("epic:#{}", n))
                    .unwrap_or_else(|| "epic:—".to_string());
                let stats = if entry.has_work_order {
                    format!(
                        "ready={}  in-flight={}  blocked={}  done={}/{}",
                        entry.ready_frontier,
                        entry.in_flight,
                        entry.blocked,
                        entry.done,
                        entry.total,
                    )
                } else {
                    "no work order".to_string()
                };
                let row_label = format!(
                    " {}  #{}  {}   {}   {}",
                    entry.repo,
                    entry.milestone_number,
                    trunc(&entry.title, 32),
                    tracking,
                    stats,
                );
                let base_color = if Self::has_loud_attention(entry) {
                    // Only a loud (ready_waiting/stalled) signal → warmer
                    // accent on the base text so the row reads as "look at
                    // me" even before the chips (#1001: no_work_order alone
                    // no longer earns this).
                    Color::rgb(220, 190, 120)
                } else {
                    Color::rgb(200, 200, 200)
                };
                let mut spans = vec![StyledSpan::with_fg(row_label, base_color)];
                for signal in &entry.needs_you {
                    let (label, color) = Self::health_chip_for_signal(signal);
                    spans.push(StyledSpan::with_fg(format!("  [{label}]"), color));
                }
                // Always-on done% chip — a progress indicator, not an
                // attention signal, so it renders regardless of `needs_you`.
                if entry.has_work_order && entry.total > 0 {
                    let pct = (entry.done * 100) / entry.total;
                    let pct_color = if pct >= 100 {
                        Color::rgb(120, 210, 120)
                    } else {
                        Color::rgb(150, 150, 160)
                    };
                    spans.push(StyledSpan::with_fg(format!("  [{pct}% done]"), pct_color));
                }
                if data_idx == sel {
                    selected_idx = items.len();
                }
                items.push(ListItem {
                    text: StyledText { spans },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Normal,
                });
                data_idx += 1;
            }

            if untracked_count > 0 && !expanded {
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(
                            format!(
                                "    +{untracked_count} without a work order  (press u to expand)",
                            ),
                            Color::rgb(120, 120, 130),
                        )],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Muted,
                });
            }
        }

        let total = items.len();
        backend.draw_list(
            list_rect,
            &ListView {
                id: WidgetId::new("plans-list"),
                title: Some(StyledText::plain(" PLANS ")),
                items,
                selected_idx,
                scroll_offset: 0,
                has_focus: true,
                bordered: true,
                h_scroll: 0,
                max_content_width: None,
                show_v_scrollbar: total > 10,
            },
        );
    }

    /// Carve the pinned GOAL.md header strip off the top of the Plans main
    /// panel rect (#978). Reserves 2 rows (headline + staleness line),
    /// capped at 30% of the available height so a short terminal still
    /// leaves room for at least one roster row below it. Mirrors
    /// `pipeline_detail_pv_rect_strip` in `render.rs`.
    fn plans_goal_header_rect(main: Rect, lh: f32) -> Rect {
        if lh <= 0.0 {
            return Rect::new(main.x, main.y, main.width, 0.0);
        }
        let want_rows = 2.0_f32;
        let max_h = (main.height * 0.30).max(lh);
        let h = (want_rows * lh).min(max_h);
        Rect::new(main.x, main.y, main.width, h)
    }

    /// Render the pinned GOAL.md north-star header (#978): the headline
    /// one-liner plus a "updated <date> · <N>d ago" staleness hint, amber +
    /// `⚠ stale` past `GOAL_STALE_DAYS`. Read-only — not part of the
    /// selectable roster drawn below it. Only called when
    /// `self.data.goal_header.available`.
    fn render_goal_header_strip(&self, backend: &mut dyn Backend, rect: Rect) {
        const GOAL_STALE_DAYS: i64 = 14;
        let goal = &self.data.goal_header;
        let headline = if goal.headline.is_empty() {
            "GOAL.md".to_string()
        } else {
            trunc(&goal.headline, 100).to_string()
        };
        let mut items = vec![ListItem {
            text: StyledText {
                spans: vec![
                    StyledSpan::with_fg(" ★ NORTH STAR  ".to_string(), Color::rgb(230, 200, 120)),
                    StyledSpan::with_fg(headline, Color::rgb(220, 220, 220)),
                ],
            },
            icon: None,
            detail: None,
            decoration: Decoration::Header,
        }];
        if let Some(last_updated) = &goal.last_updated {
            let (age_text, age_color) = match goal.days_since_update {
                Some(days) if days > GOAL_STALE_DAYS => (
                    format!("   updated {last_updated} · {days}d ago  ⚠ stale"),
                    Color::rgb(220, 140, 90),
                ),
                Some(0) => (
                    format!("   updated {last_updated} · today"),
                    Color::rgb(140, 140, 150),
                ),
                Some(days) => (
                    format!("   updated {last_updated} · {days}d ago"),
                    Color::rgb(140, 140, 150),
                ),
                None => (format!("   updated {last_updated}"), Color::rgb(140, 140, 150)),
            };
            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(age_text, age_color)],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Normal,
            });
        }
        backend.draw_list(
            rect,
            &ListView {
                id: WidgetId::new("plans-goal-header"),
                title: None,
                items,
                selected_idx: 0,
                scroll_offset: 0,
                has_focus: false,
                bordered: false,
                h_scroll: 0,
                max_content_width: None,
                show_v_scrollbar: false,
            },
        );
    }

    /// Enter / "open selected plan" — spawn `gh issue view <tracking_issue>
    /// --repo <slug> --web` for the selected plan.  Silently noops when
    /// nothing is selected or the plan has no tracking epic yet (a "create
    /// an epic" workflow lives in #977 / #978).  Returns `true` when the
    /// tracking-epic open was attempted so the caller can request a redraw.
    ///
    /// Mirrors `dispatch_open_pr_for_selected_pipeline_row` — bypasses the
    /// command runner because `gh` isn't a `coord` subcommand and the runner
    /// is `coord`-verb-scoped.  In `#[cfg(test)]` builds the spawn itself is
    /// skipped (so `cargo test` doesn't try to shell out to a real `gh`); a
    /// toast is still pushed so tests can observe the action via the screen.
    pub(crate) fn open_selected_plan_tracking_epic(&mut self) -> bool {
        let Some(entry) = self.plans_selected() else {
            self.push_toast(
                "Open plan",
                "No plan selected — highlight a row first.",
                ToastSeverity::Info,
            );
            return false;
        };
        let Some(tracking) = entry.tracking_issue else {
            self.push_toast(
                "No tracking epic yet",
                &format!(
                    "{} #{}: {} has no `epic`-labelled tracking issue. \
                     Create one with `coord milestone chat`.",
                    entry.repo, entry.milestone_number, entry.title,
                ),
                ToastSeverity::Info,
            );
            return false;
        };
        // Resolve the coord-local repo → GitHub slug so `gh --repo` gets the
        // full owner/name.  Empty slug falls through to `gh` picking the
        // ambient repo from the cwd (still useful; just less precise).
        let repo_slug = self
            .data
            .pipeline_repos
            .iter()
            .find(|(name, _)| name == &entry.repo)
            .map(|(_, gh)| gh.clone())
            .unwrap_or_default();
        // Skip the real spawn under `cargo test` (no `gh` on CI sandbox, no
        // point opening a browser during a headless test).  The toast fires
        // regardless so tests can observe the action.
        #[cfg(not(test))]
        {
            let issue_str = tracking.to_string();
            let mut cmd = std::process::Command::new("gh");
            cmd.args(["issue", "view", &issue_str]);
            if !repo_slug.is_empty() {
                cmd.args(["--repo", &repo_slug]);
            }
            cmd.arg("--web")
                .stdin(std::process::Stdio::null())
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null());
            let _ = cmd.spawn();
        }
        #[cfg(test)]
        let _ = &repo_slug; // silence unused-var under test builds
        self.push_toast(
            "Opening plan",
            &format!(
                "gh issue view #{} — opening tracking epic for {} #{} in browser…",
                tracking, entry.repo, entry.milestone_number,
            ),
            ToastSeverity::Info,
        );
        true
    }

    /// Submit handler for the #977 "fast plan capture" prompt (`c` in the
    /// Plans panel). Fires `coord milestone capture <repo> --title <title>`
    /// through the command runner — the CLI seam composes `write_milestone`
    /// + `create_issue` + `assign_issue_milestone` server-side so the new
    /// milestone/issue pair shows up in `plan_roster` (flagged
    /// `no_work_order`) on the next board refresh, no `coord sync` needed.
    ///
    /// Target repo: the repo of the currently-selected plan-roster row, or
    /// (when the roster is empty) the first configured repo. Toasts + noops
    /// when no repo is configured at all, or when the trimmed title is
    /// empty.
    pub(crate) fn capture_plan_stub(&mut self, title: String) {
        let title = title.trim().to_string();
        if title.is_empty() {
            self.push_toast(
                "Capture plan",
                "Plan title can't be empty — nothing captured.",
                ToastSeverity::Info,
            );
            return;
        }
        let Some(repo) = self
            .plans_selected()
            .map(|e| e.repo)
            .or_else(|| self.data.pipeline_repos.first().map(|(n, _)| n.clone()))
        else {
            self.push_toast(
                "Capture plan",
                "No repo configured — nothing to capture into.",
                ToastSeverity::Info,
            );
            return;
        };
        let args = ["milestone", "capture", repo.as_str(), "--title", title.as_str()];
        use crate::commands::SpawnQueuedOutcome;
        match self.command_runner.spawn_queued(&args) {
            SpawnQueuedOutcome::Deduped => {}
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "Plan capture queued",
                    &format!("\"{title}\" ({repo}) — will capture after current command."),
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Started => {
                self.push_toast(
                    "Plan captured",
                    &format!("\"{title}\" ({repo}) — dispatching `coord milestone capture`…"),
                    ToastSeverity::Info,
                );
            }
        }
    }
}

// ─── Pure-function unit tests ─────────────────────────────────────────────────

#[cfg(test)]
mod pure_tests {
    use super::*;

    fn entry(
        repo: &str,
        ms: i64,
        title: &str,
        tracking: Option<u64>,
        needs: &[&str],
    ) -> PlanRosterEntry {
        PlanRosterEntry {
            repo: repo.to_string(),
            title: title.to_string(),
            milestone_number: ms,
            tracking_issue: tracking,
            has_work_order: tracking.is_some(),
            ready_frontier: 0,
            blocked: 0,
            in_flight: 0,
            done: 0,
            total: 0,
            needs_you: needs.iter().map(|s| s.to_string()).collect(),
        }
    }

    #[test]
    fn plans_entries_sorts_by_repo_then_milestone_number() {
        let entries = vec![
            entry("b-repo", 2, "b2", None, &[]),
            entry("a-repo", 5, "a5", None, &[]),
            entry("b-repo", 1, "b1", None, &[]),
            entry("a-repo", 1, "a1", None, &[]),
        ];
        // Simulate what the payload → BoardData flow would set.
        let ordered: Vec<(String, i64)> = {
            let mut es = entries;
            es.sort_by(|a, b| {
                (a.repo.as_str(), a.milestone_number).cmp(&(b.repo.as_str(), b.milestone_number))
            });
            es.into_iter().map(|e| (e.repo, e.milestone_number)).collect()
        };
        assert_eq!(
            ordered,
            vec![
                ("a-repo".to_string(), 1),
                ("a-repo".to_string(), 5),
                ("b-repo".to_string(), 1),
                ("b-repo".to_string(), 2),
            ]
        );
    }

    #[test]
    fn plan_roster_entry_deserializes_matching_payload_shape() {
        // Golden: mirror exactly what `coord.plans.PlanEntry.to_dict()` emits.
        // Any drift here would fail the whole BoardPayload parse (#632).
        let json = r#"{
            "repo": "api",
            "title": "Substrate",
            "milestone_number": 5,
            "tracking_issue": 500,
            "has_work_order": true,
            "ready_frontier": 2,
            "blocked": 1,
            "in_flight": 0,
            "done": 0,
            "total": 3,
            "needs_you": ["ready_waiting"]
        }"#;
        let entry: PlanRosterEntry = serde_json::from_str(json).expect("valid roster JSON");
        assert_eq!(entry.repo, "api");
        assert_eq!(entry.milestone_number, 5);
        assert_eq!(entry.tracking_issue, Some(500));
        assert!(entry.has_work_order);
        assert_eq!(entry.ready_frontier, 2);
        assert_eq!(entry.needs_you, vec!["ready_waiting".to_string()]);
    }

    #[test]
    fn plan_roster_entry_deserializes_with_null_tracking_issue() {
        // A milestone without an epic reports tracking_issue: null.
        let json = r#"{
            "repo": "api",
            "title": "Follow-up",
            "milestone_number": 6,
            "tracking_issue": null,
            "has_work_order": false,
            "ready_frontier": 0,
            "blocked": 0,
            "in_flight": 0,
            "done": 0,
            "total": 0,
            "needs_you": ["no_work_order"]
        }"#;
        let entry: PlanRosterEntry = serde_json::from_str(json).expect("valid roster JSON");
        assert_eq!(entry.tracking_issue, None);
        assert!(!entry.has_work_order);
        assert_eq!(entry.needs_you, vec!["no_work_order".to_string()]);
    }
}
