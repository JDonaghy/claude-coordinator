//! Pipeline state machine, structs, and merge-queue dispatch extracted from `app/mod.rs` (#745).
//!
//! Covers the pipeline-workflow state structs (ArmedAutoReview, PendingRework, etc.),
//! pipeline layout / badge helpers, and the full `impl CoordApp` pipeline-panel cluster:
//! stage helpers, lifecycle grouping, merge-queue panel, ranked plan, headless-merge
//! dispatch, pipeline sidebar rebuild, state-machine resolvers (stage_status_for, etc.),
//! artifact helpers, test-plan lifecycle, and remote-session polling.
//!
//! **Import pattern:** `use super::*` is intentional — these methods live on `CoordApp`
//! and need the full parent namespace. See `sessions.rs` for the full rationale.
#[allow(unused_imports)]
use super::*;

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
pub(crate) struct ArmedAutoReview {
    /// Coordinator-local repo name (matches `Assignment.repo`).
    pub(crate) coord_repo: String,
    /// GitHub repo slug (the `pipeline_issues` key half), for row selection.
    pub(crate) repo_slug: String,
    /// GitHub issue number.
    pub(crate) issue_num: u64,
    /// Work assignment ids already `done` + branch when we armed.  The prompt
    /// fires only when a work aid appears that is NOT in this set — i.e. the
    /// freshly-launched interactive work, not a pre-existing completion.
    pub(crate) prior_done_ids: std::collections::HashSet<String>,
}

/// Leg 2 (#517): the issue whose smoke test just passed/skipped, awaiting the
/// operator's one-key confirm to launch the human-attended review (Test
/// precedes Review).  Raised by `detect_test_verdict`.
pub(crate) struct PendingAutoReview {
    pub(crate) coord_repo: String,
    pub(crate) repo_slug: String,
    pub(crate) issue_num: u64,
}

/// Which interactive stage a [`PendingStageLaunch`] offer starts.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum StageLaunchKind {
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
pub(crate) struct PendingStageLaunch {
    pub(crate) coord_repo: String,
    pub(crate) repo_slug: String,
    pub(crate) issue_num: u64,
    pub(crate) kind: StageLaunchKind,
}

/// #685: action performed when the test-mode choice is confirmed.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum TestModeChoiceAction {
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
    pub(crate) coord_repo: String,
    pub(crate) issue_num: u64,
    /// What to do after the mode is chosen.
    pub(crate) action: TestModeChoiceAction,
    /// Currently set test-mode (from `all_labels`), used to pre-select the default.
    pub(crate) current_mode: Option<String>,
    /// `coord assign` machine name (used for DispatchWork).
    pub(crate) machine_name: Option<String>,
    /// `coord assign` model override (used for DispatchWork).
    pub(crate) model_override: Option<String>,
}

/// Leg 3 (#517): an interactive review the TUI launched this run, armed to
/// route on its verdict (board-driven, via `coord report-result`).  Fires once
/// when a NEW verdict appears (a review id not in `prior_verdicted_ids`):
/// request-changes → rework prompt; approve → smoke/merge notice.
pub(crate) struct ArmedVerdict {
    pub(crate) coord_repo: String,
    pub(crate) repo_slug: String,
    pub(crate) issue_num: u64,
    /// Review assignment ids that already carried a verdict when we armed —
    /// so only a freshly-reported verdict triggers routing.
    pub(crate) prior_verdicted_ids: std::collections::HashSet<String>,
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
pub(crate) struct PendingRework {
    pub(crate) coord_repo: String,
    pub(crate) repo_slug: String,
    pub(crate) issue_num: u64,
    /// Reviewer findings typed by the operator.  Required before the fix
    /// can be dispatched — the rework dialog blocks confirm when empty.
    pub(crate) findings: String,
}

/// Leg 3c / A3 (#517, #581): an interactive testing session the TUI launched,
/// armed to route on its verdict (board-driven, via `coord test --passed|--fail`
/// recorded on the WORK row).  Fires once when the work row's `test_state`
/// changes to a terminal value: `failed` → fail→fix prompt; `passed`/`skipped`
/// → pass→merge prompt.
pub(crate) struct ArmedTestVerdict {
    pub(crate) coord_repo: String,
    pub(crate) repo_slug: String,
    pub(crate) issue_num: u64,
    /// The WORK assignment id under test — its `test_state` carries the verdict.
    pub(crate) work_aid: String,
    /// The work row's `test_state` when we armed, so only a NEW verdict fires.
    pub(crate) prior_test_state: Option<String>,
}

/// Leg 3c (#517, #581): a failed manual test awaiting the operator's one-key
/// confirm to launch the human-attended `--fix-of` fix on the existing branch.
/// `work_aid` is the failed WORK id (the backend's #581 test-fail fix front
/// door accepts it directly).
pub(crate) struct PendingTestFix {
    pub(crate) coord_repo: String,
    pub(crate) repo_slug: String,
    pub(crate) issue_num: u64,
}

/// Leg 3c (#517, #306): a passed test awaiting the operator's one-key confirm
/// to launch the human-attended `--merge-of` merge agent (proactive rebase +
/// conflict resolution) on the approved branch.
pub(crate) struct PendingMerge {
    pub(crate) coord_repo: String,
    pub(crate) repo_slug: String,
    pub(crate) issue_num: u64,
}

/// #863: an in-flight headless preflight (`coord assign --interactive
/// --fix-of <aid> [--force] <machine> <repo> <issue> --dry-run`) checking
/// whether `pipeline.max_review_iterations` blocks a Fix dispatch, BEFORE the
/// human-attended terminal opens.  `_dispatch_fix_of`
/// (coord/commands/dispatch_workers.py) runs the same cap check and returns
/// cleanly on `--dry-run` without ever touching a TTY, so this preflight is a
/// safe, side-effect-free probe dispatched via `CommandRunner` — completely
/// separate from the real (embedded-PTY) Fix launch it gates.
///
/// Matched against the completed `CommandResult` by `work_aid` in
/// `run_periodic_work`.  On a clean exit the real launch proceeds (forced or
/// not, per `force`); on a cap refusal (`max_review_iterations` in stderr)
/// `pending_fix_force_confirm` is raised instead.
#[derive(Clone)]
pub(crate) struct PendingFixCapPreflight {
    pub(crate) coord_repo: String,
    pub(crate) repo_slug: String,
    pub(crate) issue_num: u64,
    pub(crate) machine: String,
    pub(crate) work_aid: String,
    /// Whether THIS preflight already carries `--force` (the operator's
    /// confirmed retry) — echoed back to the real launch on success so the
    /// embedded terminal's command line also gets `--force`.
    pub(crate) force: bool,
}

/// #863: the iteration cap was hit — awaiting the operator's one-key confirm
/// to re-dispatch the SAME Fix with `--force` (#862's override).  Raised by
/// the `PendingFixCapPreflight` completion handler; consumed by
/// `confirm_fix_force_past_cap`.
pub(crate) struct PendingFixForceConfirm {
    pub(crate) coord_repo: String,
    pub(crate) repo_slug: String,
    pub(crate) issue_num: u64,
    pub(crate) machine: String,
    pub(crate) work_aid: String,
    /// The configured cap (`pipeline.max_review_iterations`), parsed from the
    /// preflight's stderr, for the confirm prompt text.  `None` if the
    /// stderr format ever drifts — the prompt falls back to generic wording.
    pub(crate) max_iterations: Option<u32>,
}

/// Width of one arrow connector between stages, in TUI cells. Mirrors the
/// constant used by quadraui's `tui_pipeline_view_layout` so host
/// hit-testing matches the painted geometry.
pub(crate) const PIPELINE_ARROW_WIDTH: f32 = 4.0;
/// Height of the action-button row when any stage has an action.
pub(crate) const PIPELINE_ACTION_HEIGHT: f32 = 1.0;

/// Compute the PipelineView layout that the TUI backend would paint into
/// `rect`. Lets `mouse_main_click` hit-test without holding a `Backend`.
///
/// Matches the constants used by `quadraui::tui::tui_pipeline_view_layout`;
/// if those drift, the GTK and TUI flows could disagree on stage bounds.
pub(crate) fn tui_pipeline_layout(view: &QuiPipelineView, rect: Rect) -> quadraui::PipelineViewLayout {
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
pub(crate) fn stage_badge(stage: &str, theme: &quadraui::Theme) -> (String, Color) {
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
pub(crate) fn ci_stale_secs(cached: Option<&CiCheckSummary>, merge_eligible: bool) -> Option<u64> {
    match cached {
        Some(s) if s.running > 0 => Some(30),
        Some(_) => Some(600),
        None if merge_eligible => Some(0),
        None => None,
    }
}

// ─── Pipeline impl CoordApp ─────────────────────────────────────────────────

impl CoordApp {

    // ── Pipeline panel ────────────────────────────────────────────────────

    /// Effective list of stages: a Plan stage (when `pipeline_require_plan`
    /// is set), then "work", then the configured `pipeline.default_gates`
    /// (deduplicated to handle accidental "work" / "plan" entries in the
    /// gate list).
    pub(crate) fn pipeline_stage_names(&self) -> Vec<String> {
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
    pub(crate) fn pipeline_stage_names_for_issue(&self, issue: &PipelineIssue) -> Vec<String> {
        let mut stages = self.pipeline_stage_names();
        let already_has_plan = stages.first().map(|s| s == "plan").unwrap_or(false);
        if !already_has_plan && self.issue_has_plan_assignment(issue) {
            stages.insert(0, "plan".to_string());
        }
        stages
    }

    /// True iff at least one assignment with `type="plan"` exists for
    /// *issue* (matching by issue number and, when set, coord_repo).
    pub(crate) fn issue_has_plan_assignment(&self, issue: &PipelineIssue) -> bool {
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
    pub(crate) fn pipeline_lifecycle_section(&self, issue: &PipelineIssue) -> &'static str {
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
    pub(crate) fn issue_has_any_assignment(&self, issue: &PipelineIssue) -> bool {
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
    pub(crate) fn issue_total_cost(&self, issue: &PipelineIssue) -> Option<f64> {
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
    pub(crate) fn issue_total_tokens(&self, issue: &PipelineIssue) -> i64 {
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
    pub(crate) fn pipeline_repo_key(issue: &PipelineIssue) -> &str {
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
    pub(crate) fn pipeline_groups_for_repo(&self, repo_key: &str) -> Vec<(&'static str, Vec<usize>)> {
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
    pub(crate) fn capture_pipeline_selection_id(&self) -> Option<(String, u64)> {
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
    pub(crate) fn repo_tag(repo_name: &str, all_repos: &[String]) -> String {
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
    pub(crate) fn pipeline_active_issues(&self) -> Vec<usize> {
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
    pub(crate) fn issue_session_is_live(&self, issue: &PipelineIssue) -> bool {
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
    pub(crate) fn pipeline_active_by_liveness(&self) -> Vec<(String, Vec<usize>)> {
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
    pub(crate) fn liveness_group_label(key: &str) -> &'static str {
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
    pub(crate) fn issue_done_at(&self, issue: &PipelineIssue) -> Option<f64> {
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
    pub(crate) fn pipeline_done_windowed(&self) -> Vec<usize> {
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
    pub(crate) fn is_done_section_active(&self) -> bool {
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
    pub(crate) fn pipeline_repos_for_state(&self, lc_key: &'static str) -> Vec<(String, Vec<usize>)> {
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
    pub(crate) fn pipeline_milestones_for_issues(
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
    pub(crate) fn pending_merge_queue_depth_by_slug(&self) -> std::collections::HashMap<String, usize> {
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
    pub(crate) fn merge_queue_needs_attention(&self) -> bool {
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
    pub(crate) fn selected_merge_queue_entry(&self) -> Option<&MergeQueueEntry> {
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
    pub(crate) fn selected_merge_plan_entry(&self) -> Option<&PlannedMergeEntry> {
        let n = self.data.merge_plan.len();
        if n == 0 {
            return None;
        }
        self.data.merge_plan.get(self.merge_queue_sel.min(n - 1))
    }

    /// Format a single merge-queue entry as a terse list label.
    ///
    /// Template: `[<STATE>] #<PR>  <issue_title>  (<error-reason>)`
    pub(crate) fn merge_queue_entry_label(&self, entry: &MergeQueueEntry) -> String {
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
    pub(crate) fn merge_queue_entry_color(&self, entry: &MergeQueueEntry) -> Color {
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
    pub(crate) fn merge_queue_sidebar(&self) -> ListView {
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
    pub(crate) fn render_merge_queue_panel(&self, backend: &mut dyn Backend, rect: Rect, _lh: f32) {
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
    pub(crate) fn render_merge_plan_panel(&self, backend: &mut dyn Backend, rect: Rect) {
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
    pub(crate) fn dispatch_merge_queue_merge_only(&mut self, force: bool) {
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
    pub(crate) fn dispatch_merge_queue_merge_all(&mut self) {
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
    pub(crate) fn dispatch_merge_queue_drop(&mut self) {
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
    pub(crate) fn has_active_interactive_merge_for_issue(&self, issue_num: u64) -> bool {
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
    pub(crate) fn has_active_headless_merge_for_issue(&self, issue_num: u64) -> bool {
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
    pub(crate) fn dispatch_merge_automated_for_selected_pipeline_issue(&mut self) -> bool {
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
    pub(crate) fn launch_merge_queue_interactive(&mut self) {
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
    pub(crate) fn rebuild_pipeline_sidebar(&mut self, prev_sel_override: Option<(String, u64)>) {
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
                                // #857: milestones-first view — an untouched
                                // milestone key defaults to collapsed so the
                                // New section opens showing just milestone
                                // headers; once toggled, the choice persists
                                // across rebuilds (handled by the `.get(...)`
                                // lookup below finding the stored value).
                                let is_mil_expanded = self
                                    .pipeline_milestone_expanded
                                    .get(&(
                                        lc_key.to_string(),
                                        repo_key.clone(),
                                        mil_key.clone(),
                                    ))
                                    .copied()
                                    .unwrap_or(false);
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
    pub(crate) fn selected_pipeline_index(&self) -> Option<usize> {
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
    pub(crate) fn stage_status_for(&self, issue: &PipelineIssue, stage: &str) -> StageStatus {
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
    pub(crate) fn assignments_for_stage<'a>(
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
    pub(crate) fn upstream_max_dispatched_at(&self, issue: &PipelineIssue, stage: &str) -> Option<f64> {
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
    pub(crate) fn merge_stage_status_for(&self, issue: &PipelineIssue) -> StageStatus {
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
    pub(crate) fn ci_failed_for_entry(&self, entry: &MergeQueueEntry) -> bool {
        let Some(pr) = entry.pr_number else {
            return false;
        };
        self.pipeline_ci_checks
            .get(&(entry.repo_github.clone(), pr))
            .is_some_and(|s| s.has_failures())
    }

    /// #241: is there a conflict-fix worker currently in flight for *issue*?
    pub(crate) fn has_active_conflict_fix(&self, issue: &PipelineIssue) -> bool {
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
    pub(crate) fn has_active_smoke_session(&self, issue: &PipelineIssue) -> bool {
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
    pub(crate) fn test_stage_status_for(&self, issue: &PipelineIssue) -> StageStatus {
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
    pub(crate) fn stage_status_for_internal_work(&self, issue: &PipelineIssue) -> StageStatus {
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
    pub(crate) fn derive_current_stage(&self, issue: &PipelineIssue) -> String {
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
    pub(crate) fn default_focused_stage_for_selected_issue(&self) -> Option<usize> {
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
    pub(crate) fn focus_next_pipeline_stage(&mut self) {
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
    pub(crate) fn focus_prev_pipeline_stage(&mut self) {
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
    pub(crate) fn chat_is_refinement(&self) -> bool {
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
    pub(crate) fn chat_is_board_chat(&self) -> bool {
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
    pub(crate) fn has_active_refinement_for_selected_issue(&self) -> bool {
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
    pub(crate) fn refinement_tab_placeholder_list(&self) -> ListView {
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
    pub(crate) fn pipeline_action_button(&self) -> Option<(String, usize)> {
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
    pub(crate) fn pipeline_action_bar_toolbar(&self) -> Option<Toolbar> {
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

    pub(crate) fn build_pipeline_widget(&self) -> Option<QuiPipelineView> {
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
    pub(crate) fn best_machine_for(&self, coord_repo: &str) -> Option<&Machine> {
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
    pub(crate) fn pipeline_selected_work_id(&self) -> Option<String> {
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
    pub(crate) fn record_test_verdict(&mut self, verdict: &str, reason: Option<&str>) -> bool {
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
    pub(crate) fn test_gate_actionable(&self) -> bool {
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
    pub(crate) fn is_test_stage_focused(&self) -> bool {
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
    pub(crate) fn test_plan_pull_step_idx(&self) -> Option<usize> {
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
    pub(crate) fn test_plan_runnable_step_idx(&self, key_num: usize) -> Option<usize> {
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
    pub(crate) fn artifact_fetch_target(&self) -> Option<(String, String, String, String)> {
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
    pub(crate) fn artifact_badge_visible(&self) -> bool {
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
    pub(crate) fn compute_a_key_artifact_action(&self) -> Option<AKeyArtifactAction> {
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
    pub(crate) fn can_dispatch_review_after_test_done(&self) -> bool {
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
    pub(crate) fn can_bounce_work_after_test_fail(&self) -> bool {
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
    pub(crate) fn test_build_in_flight(&self, work_id: &str) -> bool {
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
    pub(crate) fn can_trigger_test_build(&self) -> bool {
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
    pub(crate) fn spawn_test_build(&mut self, work_id: String, branch: String, issue_number: u64) -> bool {
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
    pub(crate) fn poll_test_build_jobs(&mut self) -> bool {
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
    pub(crate) fn maybe_spawn_test_plan(&mut self) {
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
    pub(crate) fn run_test_plan_step(&mut self, step_idx: usize) {
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
    pub(crate) fn poll_test_step_jobs(&mut self) -> bool {
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
    pub(crate) fn dispatch_pipeline_stage(&mut self, stage_idx: usize) -> bool {
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
    pub(crate) fn dispatch_pipeline_active_go(&mut self) -> bool {
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
    pub(crate) fn retry_pipeline_assignment(&mut self, issue: &PipelineIssue, stage: &str) -> bool {
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
    pub(crate) fn dispatch_pipeline_work(&mut self) -> bool {
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
    pub(crate) fn dispatch_pipeline_work_with_mode(
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
    pub(crate) fn dispatch_pipeline_plan(&mut self) -> bool {
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
    pub(crate) fn find_done_plan_assignment_id(
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
    pub(crate) fn dispatch_pipeline_review(&mut self) -> bool {
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
    pub(crate) fn maybe_kick_pipeline_loader(&mut self) {
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
    pub(crate) fn maybe_kick_ci_check_loaders(&mut self) {
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
    pub(crate) fn poll_ci_check_loaders(&mut self) -> bool {
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
    pub(crate) fn merge_blocked_on_review_for_selected_issue(&self) -> bool {
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
    pub(crate) fn issue_has_any_approved_review(
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
    pub(crate) fn pipeline_merge_state(&self) -> PipelineMergeState {
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
    pub(crate) fn dispatch_pipeline_merge_for_selected_issue(&mut self) -> bool {
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
    pub(crate) fn selected_pipeline_review_id_for_bounce(&self) -> Option<String> {
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
    pub(crate) fn dispatch_bounce_for_selected_pipeline_row(&mut self) -> bool {
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
    pub(crate) fn ci_summary_for_selected_issue(&self) -> Option<&CiCheckSummary> {
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
    pub(crate) fn poll_remote_sessions(&mut self) -> bool {
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
}
