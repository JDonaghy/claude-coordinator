//! Milestone work-order DAG/lane view — Phase 3 of #767 (#771).
//!
//! **Design note (client-side, zero daemon changes):** the `## Work order`
//! block (#768's convention) lives in the milestone's *tracking issue* body
//! — the issue carrying the `"epic"` label, mirroring
//! `coord.milestone_order.TRACKING_ISSUE_LABEL`. That body is already synced
//! into `OpenIssue.body` (#406 plumbing), so the whole DAG + node states can
//! be computed **client-side** from data the TUI already has — no new daemon
//! endpoint, no extra `gh`/network round trip. The pure functions below are
//! a lenient Rust port of `coord/milestone_order.py`'s `parse_work_order` /
//! `ready_frontier` grammar and semantics (lenient: malformed lines are
//! skipped rather than raising — this is a read-only display, and
//! `coord milestone order`/`write-order` remain the validating authority).
//!
//! "Dispatch milestone" reuses the already-shipped `coord milestone dispatch
//! <repo> <tracking_issue>` CLI (#769, Phase 1) via the same
//! spawn-a-subprocess pattern every other TUI action uses
//! (`CommandRunner::spawn_queued`) — no bespoke plumbing needed there either.
#[allow(unused_imports)]
use super::*;

/// Mirrors `coord.milestone_order.TRACKING_ISSUE_LABEL`.
pub(crate) const TRACKING_ISSUE_LABEL: &str = "epic";

/// One `- [ ] #N {group: A, after: #1,#2}` line from a `## Work order` block.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct WorkOrderNode {
    pub(crate) issue_number: u64,
    pub(crate) group: Option<String>,
    pub(crate) after: Vec<u64>,
}

/// A work-order node's dispatch/pipeline state, computed against the
/// current board (open/closed issue state + active assignments).
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum NodeState {
    /// Issue closed — this node has reached a terminal state.
    Done,
    /// An assignment for this issue currently has `status == "running"`.
    InFlight,
    /// Not done, not in flight, and at least one `after` dependency hasn't
    /// reached `Done` yet. Carries the still-unmet dependency issue numbers.
    Blocked(Vec<u64>),
    /// Not done, not in flight, all `after` dependencies are `Done` — part
    /// of the ready frontier `coord milestone dispatch` would pick up now.
    Ready,
}

/// A work-order node plus its computed display state and issue title.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct MilestoneDagNode {
    pub(crate) issue_number: u64,
    pub(crate) title: String,
    pub(crate) group: Option<String>,
    pub(crate) after: Vec<u64>,
    pub(crate) state: NodeState,
}

/// One milestone's parsed + state-annotated work order, ready to render.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct MilestoneDagView {
    pub(crate) repo_name: String,
    pub(crate) milestone_number: i64,
    pub(crate) milestone_title: String,
    /// Issue number of the "epic"-labelled tracking issue whose body carries
    /// the `## Work order` block — what `coord milestone dispatch` takes.
    pub(crate) tracking_issue: u64,
    pub(crate) nodes: Vec<MilestoneDagNode>,
}

// ─── Parsing (pure, mirrors coord/milestone_order.py's grammar) ──────────────

/// Parse the `## Work order` block out of a tracking issue's markdown body.
///
/// Grammar (mirrors `_HEADING_RE` / `_ITEM_RE` in `coord/milestone_order.py`):
/// a line matching `^#{1,6}\s*Work order\s*$` (case-insensitive) starts the
/// block; each following `- [ ] #N  {group: A, after: #1,#2}` line (checkbox
/// state ignored — cosmetic only, per the Python module) becomes a node;
/// a line starting with `#` ends the block (next markdown heading). Lines
/// that don't match the checklist-item shape are skipped rather than
/// raising — the TUI is a read-only consumer; `coord milestone order`/
/// `write-order` own validation (duplicate numbers, cycles, unknown keys).
pub(crate) fn parse_work_order(body: &str) -> Vec<WorkOrderNode> {
    let lines: Vec<&str> = body.lines().collect();
    let mut start: Option<usize> = None;
    for (i, line) in lines.iter().enumerate() {
        if is_work_order_heading(line.trim()) {
            start = Some(i + 1);
            break;
        }
    }
    let Some(start) = start else {
        return Vec::new();
    };

    let mut nodes = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for line in &lines[start..] {
        let stripped = line.trim();
        if stripped.is_empty() {
            continue;
        }
        if stripped.starts_with('#') {
            break; // next markdown heading — block ended
        }
        if let Some(node) = parse_item_line(stripped) {
            // First declaration wins on a duplicate; `coord milestone order`
            // is the authority that rejects duplicates outright.
            if seen.insert(node.issue_number) {
                nodes.push(node);
            }
        }
    }
    nodes
}

fn is_work_order_heading(line: &str) -> bool {
    if !line.starts_with('#') {
        return false;
    }
    line.trim_start_matches('#').trim().eq_ignore_ascii_case("Work order")
}

/// Parse one checklist line: `- [ ] #762  {group: A, after: #1,#2}`.
/// Returns `None` for anything that doesn't match the `- [ ] #N` shape.
fn parse_item_line(line: &str) -> Option<WorkOrderNode> {
    let rest = line.strip_prefix('-')?.trim_start();
    let rest = rest.strip_prefix('[')?;
    let (_checkbox, rest) = rest.split_once(']')?;
    let rest = rest.trim_start().strip_prefix('#')?;
    let digits_end = rest
        .find(|c: char| !c.is_ascii_digit())
        .unwrap_or(rest.len());
    if digits_end == 0 {
        return None;
    }
    let issue_number: u64 = rest[..digits_end].parse().ok()?;
    let tail = rest[digits_end..].trim_start();

    let mut group = None;
    let mut after = Vec::new();
    if let Some(brace_rest) = tail.strip_prefix('{') {
        if let Some((inside, _)) = brace_rest.split_once('}') {
            let (g, a) = parse_annotations(inside);
            group = g;
            after = a;
        }
    }
    Some(WorkOrderNode { issue_number, group, after })
}

/// Parse the `{group: A, after: #1,#2}` annotation body. Only two keys are
/// defined (mirrors `_parse_annotation` in the Python module); this scans
/// for each key's word-boundary position so either order (`group` before
/// `after` or vice versa) parses correctly without needing a full
/// comma-splitting grammar (the `after` value is itself comma-separated).
fn parse_annotations(inside: &str) -> (Option<String>, Vec<u64>) {
    let group_pos = find_key(inside, "group");
    let after_pos = find_key(inside, "after");

    let group = group_pos.map(|gp| {
        let value_start = gp + "group".len();
        let value_end = after_pos.filter(|&ap| ap > gp).unwrap_or(inside.len());
        inside[value_start..value_end]
            .trim_start_matches(':')
            .trim()
            .trim_end_matches(',')
            .trim()
            .to_string()
    });

    let after = after_pos
        .map(|ap| {
            let value_start = ap + "after".len();
            let value_end = group_pos.filter(|&gp| gp > ap).unwrap_or(inside.len());
            let raw = inside[value_start..value_end]
                .trim_start_matches(':')
                .trim()
                .trim_end_matches(',')
                .trim();
            raw.split(',')
                .filter_map(|chunk| chunk.trim().trim_start_matches('#').parse::<u64>().ok())
                .collect()
        })
        .unwrap_or_default();

    (group.filter(|g| !g.is_empty()), after)
}

/// Find the start index of `key` in `s` when followed (after optional
/// whitespace) by `:`, and not itself preceded by an alphanumeric char
/// (crude word-boundary check — good enough for the two-key annotation
/// grammar; a full lexer/regex crate is overkill here).
fn find_key(s: &str, key: &str) -> Option<usize> {
    let bytes = s.as_bytes();
    let mut idx = 0;
    while idx < s.len() {
        let Some(rel) = s[idx..].find(key) else { return None };
        let abs = idx + rel;
        let pre_ok = abs == 0 || !bytes[abs - 1].is_ascii_alphanumeric();
        let mut j = abs + key.len();
        while j < s.len() && bytes[j] == b' ' {
            j += 1;
        }
        let post_ok = j < s.len() && bytes[j] == b':';
        if pre_ok && post_ok {
            return Some(abs);
        }
        idx = abs + key.len();
    }
    None
}

// ─── Tracking-issue discovery + DAG/state computation ────────────────────────

/// The open issue in `repo_name`/`milestone_number` carrying the `"epic"`
/// label — the milestone's tracking issue (mirrors
/// `coord.milestone_order.TRACKING_ISSUE_LABEL` / the #645 convention).
pub(crate) fn milestone_tracking_issue<'a>(
    open_issues: &'a [OpenIssue],
    repo_name: &str,
    milestone_number: i64,
) -> Option<&'a OpenIssue> {
    open_issues.iter().find(|oi| {
        oi.repo_name == repo_name
            && oi.milestone_number == Some(milestone_number)
            && oi
                .labels
                .iter()
                .any(|l| l.eq_ignore_ascii_case(TRACKING_ISSUE_LABEL))
    })
}

/// Compute each node's [`NodeState`] against the current board: issue
/// `state`/assignments for done/in-flight, `after`-edges vs. the terminal
/// (closed) set for blocked/ready. Terminal is scoped to `repo_name` so two
/// repos can't cross-pollinate issue numbers.
///
/// **A work-order issue absent from the local open-issues cache counts as
/// terminal (done), not unknown** (#771 review finding). `coord/state.py`'s
/// `_upsert_open_issues_local` only ever prunes a row for being *closed* and
/// stale (`synced_at` older than 7 days) — a still-open issue is refreshed on
/// every sync and never ages out. So "missing entirely" is strong evidence of
/// "closed a while back," not genuine ambiguity; treating it as `Unknown`
/// (the previous behavior) made the "done" badge effectively never render for
/// realistic milestones (issues closed more than ~a week ago, whose
/// `synced_at` froze at their last *open* sync, had already aged out) and
/// made dependent nodes show as incorrectly `Blocked` on a dependency that
/// was actually done.
pub(crate) fn build_dag_nodes(
    work_order: &[WorkOrderNode],
    repo_name: &str,
    open_issues: &[OpenIssue],
    assignments: &[Assignment],
) -> Vec<MilestoneDagNode> {
    let terminal: std::collections::HashSet<u64> = work_order
        .iter()
        .filter(|n| {
            match open_issues
                .iter()
                .find(|oi| oi.repo_name == repo_name && oi.number == n.issue_number)
            {
                Some(oi) => oi.state == "closed",
                None => true, // aged out of the sync cache ⇒ presumed closed/done.
            }
        })
        .map(|n| n.issue_number)
        .collect();

    work_order
        .iter()
        .map(|n| {
            let found = open_issues
                .iter()
                .find(|oi| oi.repo_name == repo_name && oi.number == n.issue_number);
            let state = if terminal.contains(&n.issue_number) {
                NodeState::Done
            } else if assignments
                .iter()
                .any(|a| a.repo == repo_name && a.issue_number == n.issue_number && a.status == "running")
            {
                NodeState::InFlight
            } else {
                let unmet: Vec<u64> = n
                    .after
                    .iter()
                    .copied()
                    .filter(|d| !terminal.contains(d))
                    .collect();
                if unmet.is_empty() {
                    NodeState::Ready
                } else {
                    NodeState::Blocked(unmet)
                }
            };
            MilestoneDagNode {
                issue_number: n.issue_number,
                title: found
                    .map(|oi| oi.title.clone())
                    .unwrap_or_else(|| format!("#{}", n.issue_number)),
                group: n.group.clone(),
                after: n.after.clone(),
                state,
            }
        })
        .collect()
}

/// Group nodes by `group` cohort, preserving first-seen order (ungrouped
/// nodes form their own bucket, wherever they first appear).
pub(crate) fn nodes_by_cohort(
    nodes: &[MilestoneDagNode],
) -> Vec<(Option<String>, Vec<&MilestoneDagNode>)> {
    let mut order: Vec<Option<String>> = Vec::new();
    let mut map: std::collections::HashMap<Option<String>, Vec<&MilestoneDagNode>> =
        std::collections::HashMap::new();
    for n in nodes {
        let key = n.group.clone();
        if !map.contains_key(&key) {
            order.push(key.clone());
        }
        map.entry(key).or_default().push(n);
    }
    order
        .into_iter()
        .map(|k| {
            let v = map.remove(&k).unwrap_or_default();
            (k, v)
        })
        .collect()
}

/// Every milestone (any repo) whose tracking issue carries a non-empty
/// `## Work order` block, sorted by `(repo_name, milestone_number)`.
pub(crate) fn milestones_with_work_orders(
    open_issues: &[OpenIssue],
    assignments: &[Assignment],
) -> Vec<MilestoneDagView> {
    let mut seen: std::collections::BTreeSet<(String, i64)> = std::collections::BTreeSet::new();
    let mut out = Vec::new();
    for oi in open_issues {
        let Some(mn) = oi.milestone_number else { continue };
        let key = (oi.repo_name.clone(), mn);
        if !seen.insert(key) {
            continue;
        }
        let Some(tracking) = milestone_tracking_issue(open_issues, &oi.repo_name, mn) else {
            continue;
        };
        let work_order = parse_work_order(&tracking.body);
        if work_order.is_empty() {
            continue;
        }
        let nodes = build_dag_nodes(&work_order, &oi.repo_name, open_issues, assignments);
        out.push(MilestoneDagView {
            repo_name: oi.repo_name.clone(),
            milestone_number: mn,
            milestone_title: tracking.milestone_title.clone().unwrap_or_default(),
            tracking_issue: tracking.number,
            nodes,
        });
    }
    out.sort_by(|a, b| {
        (a.repo_name.as_str(), a.milestone_number).cmp(&(b.repo_name.as_str(), b.milestone_number))
    });
    out
}

fn milestone_node_badge(state: &NodeState, theme: &quadraui::Theme) -> (&'static str, Color) {
    match state {
        NodeState::Done => ("done", theme.badge_passed),
        NodeState::InFlight => ("in-flight", theme.link_fg),
        NodeState::Ready => ("ready", theme.accent_fg),
        NodeState::Blocked(_) => ("blocked", theme.badge_request_changes),
    }
}

// ─── impl CoordApp — sidebar/main-panel rendering + actions ──────────────────

impl CoordApp {
    /// Every milestone-with-work-order across all synced repos, in stable
    /// `(repo_name, milestone_number)` order. Recomputed on demand (cheap —
    /// linear scan + string parse over the already-in-memory open-issues
    /// cache) rather than cached, so it never goes stale after a refresh.
    pub(crate) fn milestone_dag_views(&self) -> Vec<MilestoneDagView> {
        milestones_with_work_orders(&self.data.open_issues, &self.data.assignments)
    }

    /// The currently-selected milestone (`milestone_dag_sel`, clamped), or
    /// `None` when no milestone has a parsed work order.
    pub(crate) fn milestone_dag_selected(&self) -> Option<MilestoneDagView> {
        let views = self.milestone_dag_views();
        if views.is_empty() {
            return None;
        }
        let idx = self.milestone_dag_sel.min(views.len() - 1);
        views.into_iter().nth(idx)
    }

    /// Sidebar placeholder for the Milestone DAG view — all content lives in
    /// the main panel (mirrors `merge_queue_sidebar`'s "count + hint" shape).
    pub(crate) fn milestone_dag_sidebar(&self) -> ListView {
        let n = self.milestone_dag_views().len();
        let hint = format!(
            "  {} milestone{} with a work order",
            n,
            if n == 1 { "" } else { "s" }
        );
        ListView {
            id: WidgetId::new("milestonedag-sidebar"),
            title: Some(StyledText::plain(" MILESTONES ")),
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

    /// Render the Milestone DAG main panel: every milestone-with-work-order
    /// stacked as a header row (repo — title (#tracking) + the "Dispatch
    /// milestone [d]" hint) followed by its nodes grouped into cohort rows
    /// (`group A` / `group B` / ungrouped), each node showing its state
    /// badge and — when blocked — which issue(s) it's waiting on. The
    /// currently-selected milestone's header is highlighted via
    /// `selected_idx` so `d`/right-click "Dispatch milestone" has a visible
    /// target.
    pub(crate) fn render_milestone_dag_panel(&self, backend: &mut dyn Backend, rect: Rect, _lh: f32) {
        let views = self.milestone_dag_views();
        if views.is_empty() {
            backend.draw_list(
                rect,
                &plain_list(
                    "milestonedag-empty",
                    "  No milestone tracking issue with a `## Work order` block found.",
                    0,
                ),
            );
            return;
        }
        let sel = self.milestone_dag_sel.min(views.len() - 1);
        let theme = &self.active_theme;
        let mut items: Vec<ListItem> = Vec::new();
        let mut selected_idx = 0usize;

        for (vi, view) in views.iter().enumerate() {
            if vi == sel {
                selected_idx = items.len();
            }
            let header_label = format!(
                " {} — {} (#{})  ·  Dispatch milestone [d] ",
                view.repo_name, view.milestone_title, view.tracking_issue
            );
            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(header_label, Color::rgb(150, 190, 230))],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Header,
            });

            for (group, nodes) in nodes_by_cohort(&view.nodes) {
                let group_label = match &group {
                    Some(g) => format!("  cohort: {}", g),
                    None => "  ungrouped".to_string(),
                };
                items.push(activity_item(&group_label, Color::rgb(140, 140, 140)));

                for node in nodes {
                    let (state_text, color) = milestone_node_badge(&node.state, theme);
                    let mut spans = vec![
                        StyledSpan::with_fg(
                            format!("    #{:<5} {}", node.issue_number, trunc(&node.title, 40)),
                            Color::rgb(200, 200, 200),
                        ),
                        StyledSpan::with_fg(format!("  [{}]", state_text), color),
                    ];
                    if let NodeState::Blocked(on) = &node.state {
                        let deps = on
                            .iter()
                            .map(|n| format!("#{}", n))
                            .collect::<Vec<_>>()
                            .join(", ");
                        spans.push(StyledSpan::with_fg(
                            format!("  blocked on {}", deps),
                            Color::rgb(200, 140, 60),
                        ));
                    }
                    items.push(ListItem {
                        text: StyledText { spans },
                        icon: None,
                        detail: None,
                        decoration: Decoration::Normal,
                    });
                }
            }
        }

        let total = items.len();
        backend.draw_list(
            rect,
            &ListView {
                id: WidgetId::new("milestonedag-list"),
                title: Some(StyledText::plain(" MILESTONE WORK ORDER ")),
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

    /// Context-menu items for a milestone header row (right-click, or the
    /// keyboard shortcut / Menu key on the selected header).
    pub(crate) fn context_menu_items_for_milestone_header(
        &self,
        _repo_name: &str,
        _tracking_issue: u64,
        _milestone_title: &str,
    ) -> Vec<ContextMenuItem> {
        vec![
            ContextMenuItem::action("dispatch-milestone", "Dispatch milestone").with_shortcut("d"),
            ContextMenuItem::separator(),
            ContextMenuItem::action("refresh", "Refresh").with_shortcut("r"),
        ]
    }

    /// Promote the target milestone's whole declared work order into the
    /// pipeline: `coord milestone dispatch <repo> <tracking_issue>` (#769,
    /// Phase 1 — already shipped; this just spawns it the same way every
    /// other TUI action spawns a `coord` subcommand).
    pub(crate) fn dispatch_milestone_action(&mut self, target: &ContextMenuTarget) -> bool {
        let (repo, tracking_issue, title) = match target {
            ContextMenuTarget::MilestoneHeader {
                repo_name,
                tracking_issue,
                milestone_title,
            } => (repo_name.clone(), *tracking_issue, milestone_title.clone()),
            _ => {
                self.push_toast(
                    "Dispatch milestone unavailable",
                    "No milestone target — focus a milestone header first.",
                    ToastSeverity::Info,
                );
                return false;
            }
        };
        let issue_str = tracking_issue.to_string();
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self
            .command_runner
            .spawn_queued(&["milestone", "dispatch", &repo, &issue_str]);
        match outcome {
            SpawnQueuedOutcome::Deduped => {}
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "Dispatch milestone",
                    &format!("{}: queued — will run after current command.", title),
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Started => {
                self.push_toast(
                    "Dispatch milestone",
                    &format!("{}: dispatching ready frontier…", title),
                    ToastSeverity::Info,
                );
            }
        }
        true
    }
}

// ─── Pure-function unit tests ─────────────────────────────────────────────────

#[cfg(test)]
mod pure_tests {
    use super::*;

    fn issue(repo: &str, number: u64, title: &str, state: &str, labels: &[&str]) -> OpenIssue {
        OpenIssue {
            repo_name: repo.to_string(),
            number,
            title: title.to_string(),
            body: String::new(),
            labels: labels.iter().map(|s| s.to_string()).collect(),
            state: state.to_string(),
            milestone_number: None,
            milestone_title: None,
        }
    }

    fn make_assignment(repo: &str, issue_number: u64, status: &str) -> Assignment {
        Assignment {
            id: "a1".to_string(),
            repo: repo.to_string(),
            issue_number,
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
            test_plan: None,
            test_plan_branch_head: None,
            input_tokens: 0,
            output_tokens: 0,
            cache_creation_tokens: 0,
            cache_read_tokens: 0,
            is_interactive: false,
            failure_reason: None,
            review_iteration: 0,
        }
    }

    #[test]
    fn parse_work_order_basic() {
        let body = "\
Some intro text.

## Work order
- [ ] #762  {group: A}        # cohort A
- [ ] #763  {group: A}
- [x] #765  {after: #762,#763}   # hard dependency edge
- [ ] #766  {after: #765}

## Notes
Not part of the block.
";
        let nodes = parse_work_order(body);
        assert_eq!(nodes.len(), 4);
        assert_eq!(nodes[0].issue_number, 762);
        assert_eq!(nodes[0].group.as_deref(), Some("A"));
        assert!(nodes[0].after.is_empty());
        assert_eq!(nodes[2].issue_number, 765);
        assert_eq!(nodes[2].after, vec![762, 763]);
        assert_eq!(nodes[3].after, vec![765]);
    }

    #[test]
    fn parse_work_order_no_heading_returns_empty() {
        assert!(parse_work_order("just a normal issue body, no work order here").is_empty());
    }

    #[test]
    fn parse_work_order_group_after_either_order() {
        // `after` before `group` in the annotation body.
        let body = "## Work order\n- [ ] #1  {after: #2, group: B}\n- [ ] #2\n";
        let nodes = parse_work_order(body);
        assert_eq!(nodes[0].group.as_deref(), Some("B"));
        assert_eq!(nodes[0].after, vec![2]);
    }

    #[test]
    fn parse_work_order_skips_malformed_lines() {
        let body = "## Work order\n- not a valid item\n- [ ] #5\n";
        let nodes = parse_work_order(body);
        assert_eq!(nodes.len(), 1);
        assert_eq!(nodes[0].issue_number, 5);
    }

    #[test]
    fn milestone_tracking_issue_requires_epic_label_and_milestone_match() {
        let mut a = issue("repo", 100, "Epic", "open", &["epic", "coord"]);
        a.milestone_number = Some(5);
        let b = issue("repo", 101, "Not an epic", "open", &["coord"]);
        let issues = vec![a, b];
        let found = milestone_tracking_issue(&issues, "repo", 5);
        assert_eq!(found.map(|i| i.number), Some(100));
        assert!(milestone_tracking_issue(&issues, "repo", 99).is_none());
    }

    #[test]
    fn build_dag_nodes_computes_done_inflight_blocked_ready() {
        let work_order = vec![
            WorkOrderNode { issue_number: 1, group: None, after: vec![] },
            WorkOrderNode { issue_number: 2, group: None, after: vec![1] },
            WorkOrderNode { issue_number: 3, group: None, after: vec![2] },
            WorkOrderNode { issue_number: 4, group: None, after: vec![] },
        ];
        let open_issues = vec![
            issue("repo", 1, "Done one", "closed", &[]),
            issue("repo", 2, "In flight", "open", &[]),
            issue("repo", 3, "Blocked", "open", &[]),
            issue("repo", 4, "Ready", "open", &[]),
        ];
        let assignments = vec![make_assignment("repo", 2, "running")];
        let nodes = build_dag_nodes(&work_order, "repo", &open_issues, &assignments);
        assert_eq!(nodes[0].state, NodeState::Done);
        assert_eq!(nodes[1].state, NodeState::InFlight);
        assert_eq!(nodes[2].state, NodeState::Blocked(vec![2]));
        assert_eq!(nodes[3].state, NodeState::Ready);
    }

    /// #771 review finding 1: an issue absent from the local open-issues
    /// cache entirely (the effective outcome once a closed issue ages out of
    /// `coord/state.py`'s 7-day prune) must be presumed `Done`, not shown as
    /// an unhelpful `Unknown`/`?` — the local cache only ever drops rows for
    /// being closed-and-stale, never for a still-open issue.
    #[test]
    fn build_dag_nodes_presumes_done_when_issue_not_synced() {
        let work_order = vec![WorkOrderNode { issue_number: 99, group: None, after: vec![] }];
        let nodes = build_dag_nodes(&work_order, "repo", &[], &[]);
        assert_eq!(nodes[0].state, NodeState::Done);
        // Title falls back to the bare issue number when nothing is cached.
        assert_eq!(nodes[0].title, "#99");
    }

    /// #771 review finding 2: a dependency that has aged out of the cache
    /// (missing entirely, same as above) must clear the `after` edge rather
    /// than leaving the dependent node incorrectly `Blocked` on something
    /// that's actually done.
    #[test]
    fn build_dag_nodes_ready_when_dependency_aged_out_of_cache() {
        let work_order = vec![
            WorkOrderNode { issue_number: 765, group: None, after: vec![] },
            WorkOrderNode { issue_number: 766, group: None, after: vec![765] },
        ];
        // #765 is NOT present in open_issues — simulating a real closed
        // issue that's aged out of the 7-day-stale-closed prune.
        let open_issues = vec![issue("repo", 766, "Independent, ready", "open", &[])];
        let nodes = build_dag_nodes(&work_order, "repo", &open_issues, &[]);
        assert_eq!(nodes[0].state, NodeState::Done);
        assert_eq!(nodes[1].state, NodeState::Ready);
    }

    #[test]
    fn nodes_by_cohort_groups_and_preserves_order() {
        let nodes = vec![
            MilestoneDagNode {
                issue_number: 1,
                title: "a".into(),
                group: Some("A".into()),
                after: vec![],
                state: NodeState::Ready,
            },
            MilestoneDagNode {
                issue_number: 2,
                title: "b".into(),
                group: None,
                after: vec![],
                state: NodeState::Ready,
            },
            MilestoneDagNode {
                issue_number: 3,
                title: "c".into(),
                group: Some("A".into()),
                after: vec![],
                state: NodeState::Ready,
            },
        ];
        let cohorts = nodes_by_cohort(&nodes);
        assert_eq!(cohorts.len(), 2);
        assert_eq!(cohorts[0].0.as_deref(), Some("A"));
        assert_eq!(cohorts[0].1.len(), 2);
        assert_eq!(cohorts[1].0, None);
        assert_eq!(cohorts[1].1.len(), 1);
    }

    #[test]
    fn milestones_with_work_orders_finds_and_sorts() {
        let mut epic = issue("repo", 100, "Epic", "open", &["epic"]);
        epic.milestone_number = Some(5);
        epic.milestone_title = Some("v0.5".to_string());
        epic.body = "## Work order\n- [ ] #101\n".to_string();
        let mut child = issue("repo", 101, "Child", "open", &[]);
        child.milestone_number = Some(5);
        let issues = vec![epic, child];
        let views = milestones_with_work_orders(&issues, &[]);
        assert_eq!(views.len(), 1);
        assert_eq!(views[0].tracking_issue, 100);
        assert_eq!(views[0].milestone_title, "v0.5");
        assert_eq!(views[0].nodes.len(), 1);
    }

    #[test]
    fn milestones_without_work_order_block_are_skipped() {
        let mut epic = issue("repo", 100, "Epic", "open", &["epic"]);
        epic.milestone_number = Some(5);
        epic.body = "just a normal description, no work order".to_string();
        let views = milestones_with_work_orders(&[epic], &[]);
        assert!(views.is_empty());
    }
}
