// Sealed acceptance slice for **issue #1116** — "coord-tui: Usage view —
// per-issue cost/token grid with per-stage drill (reuses #1115 aggregation)"
// — milestone ms-37 (tracking issue #1117, Spend & Time Observability epic).
//
// Authored independently from `tests/acceptance/ms-37/contract.md` (Gate A)
// plus issue #1116's own body (the test-author brief explicitly permits
// "the contract and the issue description alone" as sources), with **zero**
// worker/implementation context. Drives the whole app through the real
// `event -> handle -> render` path via quadraui's `TuiDriver` against
// ratatui's headless `TestBackend`, exactly like the in-crate `#[cfg(test)]`
// suite and `tests/acceptance/ms-33/audit_1039.rs` (docs/ORACLE_LOOP.md,
// coord-tui `tui-tuidriver` driver).
//
// This file is `include!`d at crate root by `tui/tests/acceptance.rs`. It is
// compiled only under `--features test-support`. It is SEALED: the worker
// implementing #1116 may run it (`coord acceptance run --issue 1116`) but
// may not read or edit it.
//
// ── Scope note: contract.md's TUI section is a one-line pointer ───────────
// Unlike ms-33's Audit contract (which pinned an exact icon glyph "§", panel
// id, and title "AUDIT"), ms-37's contract.md gives the CLI's exact stdout
// mocks (Mock 1-4) plus the shared fixture + estimate/window semantics, but
// says only: "`.screen` grid mocks (same fixture, TUI render) → #1116 TUI"
// — no glyph, panel id, or navigation is pinned for the TUI surface. Per the
// test-author brief ("if the contract is ambiguous... don't guess... write
// with a TODO"), the UI-mechanics choices below are TEST-AUTHOR DECISIONS,
// not derivations from a pinned mock — flagged explicitly (and confirmed
// with the human operator mid-session) so a reviewer/coordinator can
// course-correct them in a later JIT round if the implementor's natural
// design differs:
//
//   * Panel: a NEW top-level activity-bar panel (icon "¤", title "USAGE"),
//     NOT a Pipeline sub-tab, even though the issue text offers "new
//     Pipeline sub-tab, or an extension of the existing #876 Summary tab"
//     as alternatives. This is deliberate, not arbitrary: today's public
//     `test-support` seam (`make_app_with_assignments`) can seed
//     `data.assignments` but has NO way to populate `pipeline_issues` /
//     `pipeline_sel` from an external acceptance crate (no fixture helper
//     exists, and the fields are module-private, confirmed by inspection of
//     `tui/src/app/mod.rs` and `tui/src/app/fixtures.rs`) — so a
//     per-issue-selection Pipeline sub-tab would be **unreachable** by a
//     sealed external test, exactly the seam gap `audit_1039.rs` hit for
//     its recent-badge case. A new top-level panel sourced straight from
//     `data.assignments` is the only interpretation of requirement 1 (a
//     cross-issue grid, not a single-issue view like the existing Summary
//     tab) that is actually testable with today's seams. "¤" (currency
//     sign) was chosen over the more obvious "$" specifically because "$"
//     risks colliding with existing dollar-cost text rendered elsewhere in
//     the app (assignments already carry `cost_usd`), which would make
//     `find("$")` non-deterministic; "¤" does not collide with any rendered
//     dollar amount ("$2.0000" etc.) while still reading as a currency
//     glyph, consistent with the app's existing single-glyph icon
//     convention (▶ ▦ ≣ ◆ ◉ § ⚙).
//   * Window scoping: the fixture's legs carry FIXED epoch timestamps
//     (`BASE`, mirroring `tests/acceptance/ms-37/conftest.py`'s own
//     documented technique) rather than wall-clock-relative ones, so every
//     content-assertion test scopes the view via the **custom range**
//     dialog (contract's scope-update note) instead of the `today` preset —
//     a wall-clock-relative "today" filter would make this suite flaky
//     depending on when `cargo test` runs, exactly the reasoning
//     `conftest.py`'s `BASE`/`DAY` comment gives for the CLI/Core slices.
//     The custom-range header format IS pinned by the contract's
//     scope-update note ("window: 2026-07-01 00:00 → 2026-07-08 00:00"), so
//     that part is a real derivation, not an invention.
//   * Custom-range dialog fields are located via `find("Start")` /
//     `find("End")` and a `Enter` submit, mirroring the only precedent this
//     codebase has for "type into a dialog then submit"
//     (`plans_panel_capture_key_dispatches_milestone_capture`,
//     `tui/src/app/tests.rs`) — no two-field dialog precedent exists, so
//     this is a best-effort translation of "existing quadraui primitives
//     (form / FormField / DialogTextInput)" per the issue body.
//   * Scope-preset labels ("Today" / "Week" / "Month" / "Custom range…")
//     and the group-by toggle ("Issue" / "Repo") are asserted mostly for
//     bare presence — their exact click mechanics beyond what's needed to
//     drive the custom-range flow are not pinned, since the issue leaves
//     the concrete control type unspecified ("a scope selector", no widget
//     named).
//
// If the implementor's natural design diverges from these guesses (e.g. a
// different icon, or the scope control needs an extra open-step before
// "Custom range…" is visible), that is exactly the kind of course-correction
// this milestone's JIT model expects — flag it back to the coordinator
// rather than silently reinterpreting the sealed suite.

mod usage_tui_1116 {
    use coord_tui::fixtures::{make_app_with_assignments, Assignment};
    use coord_tui::CoordApp;
    use quadraui::tui::testing::{driver_with_shell, TuiDriver};
    use quadraui::NamedKey;

    /// Same deterministic epoch anchor as `tests/acceptance/ms-37/
    /// conftest.py` (`BASE`/`DAY`) — an explicit interval, not "today"
    /// relative to the wall clock, so this suite never flakes depending on
    /// when `cargo test` runs. All six legs land within `[BASE, BASE +
    /// 6100]`, on the same local calendar day (2023-11-14 in this
    /// environment's zone, safely clear of any DST transition).
    #[allow(dead_code)]
    const BASE_DOC: f64 = 1_700_000_000.0; // = ASSIGNMENTS_JSON's L1 dispatched_at, documentation only

    /// Local calendar-day bounds around `BASE_DOC`, entered into the
    /// custom-range dialog. Every leg's `dispatched_at`/`finished_at`
    /// (offsets 0..=6000s from `BASE_DOC`) falls within this window. The
    /// resulting header format is pinned by the contract's scope-update
    /// note (`"window: <start> → <end>"`).
    const RANGE_START: &str = "2023-11-14 00:00";
    const RANGE_END: &str = "2023-11-15 00:00";

    /// Contract "Fixture — seeded board": 6 legs, 2 issues, 2 repos, mixed
    /// interactive/non-interactive, one unknown-model leg (L5), one running
    /// leg (L6, no `finished_at`). Values match
    /// `tests/acceptance/ms-37/conftest.py::BOARD_ROWS` exactly (same
    /// contract fixture, ported to the `Assignment` wire shape) so the TUI
    /// and CLI/Core slices of this milestone can never silently diverge.
    ///
    /// `Assignment`'s fields are `pub(crate)` — by design (see the doc
    /// comment on the struct in `tui/src/app/types.rs`): "nothing outside
    /// the crate constructs one field-by-field, only via `app::fixtures`
    /// helpers or `serde::Deserialize`". None of the existing `test-support`
    /// fixture helpers accept custom cost/token/timestamp values, so this
    /// slice uses the OTHER pinned seam directly: JSON matching the `/board`
    /// wire shape (`assignment_id` / `repo_name` / `machine_name` / `type`
    /// renames, everything else same-named), deserialized straight into
    /// `Assignment` via `serde::Deserialize`.
    const ASSIGNMENTS_JSON: &str = r#"[
        {"assignment_id":"L1","repo_name":"alpha","issue_number":501,"issue_title":"Alpha feature","machine_name":"testmachine","status":"merged","type":"work","model":"sonnet","cost_usd":0.50,"input_tokens":10000,"output_tokens":100000,"cache_read_tokens":1000000,"cache_creation_tokens":0,"is_interactive":false,"dispatched_at":1700000000.0,"finished_at":1700000600.0},
        {"assignment_id":"L2","repo_name":"alpha","issue_number":501,"issue_title":"Alpha feature","machine_name":"testmachine","status":"done","type":"review","model":"sonnet","cost_usd":null,"input_tokens":2000,"output_tokens":50000,"cache_read_tokens":500000,"cache_creation_tokens":0,"is_interactive":true,"dispatched_at":1700001000.0,"finished_at":1700001300.0},
        {"assignment_id":"L3","repo_name":"beta","issue_number":502,"issue_title":"Beta feature","machine_name":"testmachine","status":"merged","type":"work","model":"opus","cost_usd":2.00,"input_tokens":20000,"output_tokens":200000,"cache_read_tokens":2000000,"cache_creation_tokens":0,"is_interactive":false,"dispatched_at":1700002000.0,"finished_at":1700003200.0},
        {"assignment_id":"L4","repo_name":"beta","issue_number":502,"issue_title":"Beta feature","machine_name":"testmachine","status":"done","type":"smoke","model":"sonnet","cost_usd":null,"input_tokens":4000,"output_tokens":80000,"cache_read_tokens":800000,"cache_creation_tokens":0,"is_interactive":true,"dispatched_at":1700004000.0,"finished_at":1700004400.0},
        {"assignment_id":"L5","repo_name":"beta","issue_number":502,"issue_title":"Beta feature","machine_name":"testmachine","status":"done","type":"chat","model":"(unknown)","cost_usd":null,"input_tokens":1000,"output_tokens":30000,"cache_read_tokens":300000,"cache_creation_tokens":0,"is_interactive":true,"dispatched_at":1700005000.0,"finished_at":1700005200.0},
        {"assignment_id":"L6","repo_name":"beta","issue_number":502,"issue_title":"Beta feature","machine_name":"testmachine","status":"running","type":"work","model":"sonnet","cost_usd":null,"input_tokens":0,"output_tokens":0,"cache_read_tokens":0,"cache_creation_tokens":0,"is_interactive":false,"dispatched_at":1700006000.0,"finished_at":null}
    ]"#;

    fn seeded_assignments() -> Vec<Assignment> {
        serde_json::from_str::<Vec<Assignment>>(ASSIGNMENTS_JSON)
            .expect("fixture JSON must deserialize into Vec<Assignment> via the pinned /board wire shape")
    }

    fn usage_driver() -> TuiDriver<impl quadraui::AppLogic> {
        let app = make_app_with_assignments(seeded_assignments());
        driver_with_shell(app, CoordApp::shell_config(), 140, 40)
    }

    /// Click the `¤` activity-bar icon into the Usage panel (test-author
    /// pinned; see the scope note above) and render.
    fn nav_to_usage<A: quadraui::AppLogic>(driver: &mut TuiDriver<A>) {
        let (x, y) = driver.find("¤").expect(
            "test-author pin: the activity bar must render a '¤' Usage \
             panel icon (#1116 not landed, or a different icon/id was \
             chosen — see the scope note at the top of this file).",
        );
        assert!(
            x < 3.0,
            "the '¤' Usage icon must live in the activity-bar columns 0-2 \
             (x < 3.0); found x = {x}",
        );
        driver.click(x, y);
        driver.render();
    }

    /// Open the "Custom range…" scope control and scope the view to
    /// `[RANGE_START, RANGE_END)` — the deterministic stand-in for the
    /// fixture's "today" (see the scope note). Must be called after
    /// `nav_to_usage`.
    fn scope_to_fixture_range<A: quadraui::AppLogic>(driver: &mut TuiDriver<A>) {
        let (x, y) = driver.find("Custom range").unwrap_or_else(|| {
            panic!(
                "requirement (scope-update note): the Usage view's scope \
                 selector must offer a 'Custom range…' option.\n\
                 --- screen ---\n{}",
                driver.screen(),
            )
        });
        driver.click(x, y);
        driver.render();

        let (sx, sy) = driver.find("Start").unwrap_or_else(|| {
            panic!(
                "requirement (scope-update note): the custom-range dialog \
                 must have a 'Start' field.\n--- screen ---\n{}",
                driver.screen(),
            )
        });
        driver.click(sx, sy);
        driver.render();
        for ch in RANGE_START.chars() {
            driver.type_char(ch);
        }

        let (ex, ey) = driver.find("End").unwrap_or_else(|| {
            panic!(
                "requirement (scope-update note): the custom-range dialog \
                 must have an 'End' field.\n--- screen ---\n{}",
                driver.screen(),
            )
        });
        driver.click(ex, ey);
        driver.render();
        for ch in RANGE_END.chars() {
            driver.type_char(ch);
        }

        driver.press_named(NamedKey::Enter);
        driver.render();
    }

    /// Build a driver already on the Usage grid, scoped to the fixture's
    /// deterministic window. Shared setup for every content assertion
    /// below.
    fn scoped_usage_driver() -> TuiDriver<impl quadraui::AppLogic> {
        let mut driver = usage_driver();
        nav_to_usage(&mut driver);
        scope_to_fixture_range(&mut driver);
        driver
    }

    /// Return the first rendered line containing `needle`, or panic with
    /// the full screen for debugging.
    fn line_containing<'a>(screen: &'a str, needle: &str) -> &'a str {
        screen.lines().find(|l| l.contains(needle)).unwrap_or_else(|| {
            panic!("expected a rendered line containing {needle:?}\n--- screen ---\n{screen}")
        })
    }

    /// Whitespace-tokenized exact match — used instead of raw `contains` so
    /// a bare value like `"4"` (legs) can't accidentally match a substring
    /// of an unrelated token like `"~$1.4520"` (which also contains the
    /// digit `4`).
    fn has_token(line: &str, tok: &str) -> bool {
        line.split_whitespace().any(|t| t == tok)
    }

    // ── Panel registration / navigation ────────────────────────────────────

    #[test]
    fn usage_panel_registered_in_activity_bar() {
        let driver = usage_driver();
        let pos = driver.find("¤");
        assert!(
            pos.is_some(),
            "the activity bar must render a Usage panel icon once #1116 \
             registers it; not found.\n--- screen ---\n{}",
            driver.screen(),
        );
        let (x, _y) = pos.unwrap();
        assert!(
            x < 3.0,
            "Usage icon must be within activity-bar columns 0-2; found x = {x}",
        );
    }

    #[test]
    fn usage_header_shows_title() {
        let mut driver = usage_driver();
        nav_to_usage(&mut driver);
        assert!(
            driver.screen_contains("USAGE"),
            "with the Usage panel active, the header/title must render \
             'USAGE' (reusing the CLI's own header word, contract Mock 1: \
             \"USAGE — by issue — window: today\").\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ── Scope selector / custom range (scope-update note) ──────────────────

    #[test]
    fn scope_selector_shows_presets() {
        let mut driver = usage_driver();
        nav_to_usage(&mut driver);
        let screen = driver.screen();
        for needle in ["Today", "Week", "Month", "Custom range"] {
            assert!(
                screen.contains(needle),
                "requirement 1 + scope-update note: the Usage view's scope \
                 selector must offer {needle:?}.\n--- screen ---\n{screen}",
            );
        }
    }

    #[test]
    fn custom_range_scopes_header_to_resolved_interval() {
        let mut driver = usage_driver();
        nav_to_usage(&mut driver);
        scope_to_fixture_range(&mut driver);
        assert!(
            driver.screen_contains("window: 2023-11-14 00:00 → 2023-11-15 00:00"),
            "scope-update note: the header must reflect the resolved custom \
             range as \"window: <start> → <end>\".\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ── Per-issue grid (requirement 1, contract Mock 1 numbers) ───────────

    #[test]
    fn grid_shows_issues_desc_by_total_cost() {
        let driver = scoped_usage_driver();
        let (_x502, y502) = driver.find("#502").expect("issue #502 row must render");
        let (_x501, y501) = driver.find("#501").expect("issue #501 row must render");
        assert!(
            y502 < y501,
            "requirement 1: default sort is desc by total (captured+est) — \
             #502 (total $3.4520) must render ABOVE #501 (total $1.4060).\n\
             --- screen ---\n{}",
            driver.screen(),
        );
    }

    #[test]
    fn grid_row_502_values() {
        let driver = scoped_usage_driver();
        let screen = driver.screen();
        let line = line_containing(&screen, "#502");
        for tok in ["#502", "4", "$2.0000", "~$1.4520", "310k", "3.1M", "30m00s"] {
            assert!(
                has_token(line, tok),
                "issue #502's grid row must contain token {tok:?}.\n\
                 row: {line:?}\n--- screen ---\n{screen}",
            );
        }
        assert!(
            line.contains("unknown-model:1"),
            "issue #502's row must flag the unknown-model leg (L5) rather \
             than silently omitting its cost.\nrow: {line:?}\n--- screen ---\n{screen}",
        );
    }

    #[test]
    fn grid_row_501_values() {
        let driver = scoped_usage_driver();
        let screen = driver.screen();
        let line = line_containing(&screen, "#501");
        for tok in ["#501", "2", "$0.5000", "~$0.9060", "150k", "1.5M", "15m00s"] {
            assert!(
                has_token(line, tok),
                "issue #501's grid row must contain token {tok:?}.\n\
                 row: {line:?}\n--- screen ---\n{screen}",
            );
        }
    }

    #[test]
    fn grid_captured_and_estimated_are_visually_distinct() {
        let driver = scoped_usage_driver();
        let screen = driver.screen();
        // Requirement 3: estimated cost must render distinctly from
        // captured cost (the `~$` convention), so interactive-heavy issues
        // are never shown as flatly "$0".
        assert!(
            screen.contains("$2.0000") && screen.contains("~$1.4520"),
            "captured ($2.0000) and estimated (~$1.4520) cost for #502 must \
             both render, with the estimate carrying the '~' marker.\n\
             --- screen ---\n{screen}",
        );
    }

    #[test]
    fn grid_totals_reflect_grand_total() {
        let driver = scoped_usage_driver();
        assert!(
            driver.screen_contains("4.8580"),
            "the grand total (captured $2.5000 + est $2.3580 = $4.8580) \
             must render somewhere (a footer row if quadraui #432's \
             DataTable footer has landed, else the documented app-level \
             summary-strip fallback).\n--- screen ---\n{}",
            driver.screen(),
        );
        assert!(
            driver.screen_contains("1 in progress"),
            "L6 (running, no finished_at) must be counted as '1 in \
             progress' in the totals, not silently dropped.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ── Click-to-expand per-stage drill (requirement 2, contract Mock 2) ──

    #[test]
    fn click_row_expands_to_stage_legs() {
        let mut driver = scoped_usage_driver();
        let (x, y) = driver.find("#502").expect("issue #502 row must render");
        driver.click(x, y);
        driver.render();
        let screen = driver.screen();
        for needle in ["work", "opus", "smoke", "chat", "running"] {
            assert!(
                screen.contains(needle),
                "requirement 2: expanding #502's row must reveal its \
                 per-stage legs, including stage {needle:?}.\n--- screen ---\n{screen}",
            );
        }
    }

    #[test]
    fn drill_work_opus_leg_captured_cost() {
        let mut driver = scoped_usage_driver();
        let (x, y) = driver.find("#502").expect("issue #502 row must render");
        driver.click(x, y);
        driver.render();
        let screen = driver.screen();
        let line = line_containing(&screen, "opus");
        for tok in ["opus", "$2.0000", "200k", "2.0M", "20m00s", "merged"] {
            assert!(
                has_token(line, tok),
                "the 'work'/opus leg row must contain token {tok:?}.\n\
                 row: {line:?}\n--- screen ---\n{screen}",
            );
        }
    }

    #[test]
    fn drill_smoke_leg_nonzero_estimate() {
        let mut driver = scoped_usage_driver();
        let (x, y) = driver.find("#502").expect("issue #502 row must render");
        driver.click(x, y);
        driver.render();
        let screen = driver.screen();
        let line = line_containing(&screen, "smoke");
        // The required acceptance bullet: a non-zero ~$ estimate on an
        // interactive leg — smoke (sonnet, interactive, no captured
        // cost_usd) must show the estimated $1.4520, distinctly marked.
        for tok in ["smoke", "sonnet", "~$1.4520", "80k", "0.8M", "6m40s", "done"] {
            assert!(
                has_token(line, tok),
                "the smoke leg row must contain token {tok:?}.\n\
                 row: {line:?}\n--- screen ---\n{screen}",
            );
        }
    }

    #[test]
    fn drill_chat_leg_unknown_model_no_estimate() {
        let mut driver = scoped_usage_driver();
        let (x, y) = driver.find("#502").expect("issue #502 row must render");
        driver.click(x, y);
        driver.render();
        let screen = driver.screen();
        let line = line_containing(&screen, "chat");
        assert!(
            line.contains("unknown"),
            "the chat leg's model column must render the unknown-model \
             marker.\nrow: {line:?}\n--- screen ---\n{screen}",
        );
        for tok in ["30k", "0.3M", "3m20s"] {
            assert!(
                has_token(line, tok),
                "the chat leg row must contain token {tok:?}.\n\
                 row: {line:?}\n--- screen ---\n{screen}",
            );
        }
        assert!(
            !line.contains("~$"),
            "an unknown-model leg must NOT render a fabricated '~$' \
             estimate (contract: 'no estimate' for unknown model).\n\
             row: {line:?}\n--- screen ---\n{screen}",
        );
    }

    #[test]
    fn drill_running_leg_shows_running_status() {
        let mut driver = scoped_usage_driver();
        let (x, y) = driver.find("#502").expect("issue #502 row must render");
        driver.click(x, y);
        driver.render();
        let screen = driver.screen();
        let line = line_containing(&screen, "running");
        assert!(
            has_token(line, "work"),
            "the running leg (L6, no finished_at) is a 'work' stage.\n\
             row: {line:?}\n--- screen ---\n{screen}",
        );
    }

    // ── Group-by repo (scope-update note / requirement 1 expansion) ───────

    #[test]
    fn group_by_repo_shows_repo_rollup() {
        let mut driver = scoped_usage_driver();
        let (x, y) = driver.find("Repo").unwrap_or_else(|| {
            panic!(
                "scope-update note: the Usage view must offer a group-by \
                 (issue / repo) toggle labelled 'Repo'.\n--- screen ---\n{}",
                driver.screen(),
            )
        });
        driver.click(x, y);
        driver.render();
        let screen = driver.screen();
        let beta_line = line_containing(&screen, "beta");
        for tok in ["beta", "$2.0000", "~$1.4520", "310k", "3.1M", "30m00s"] {
            assert!(
                has_token(beta_line, tok),
                "grouped-by-repo, the 'beta' row must contain token {tok:?} \
                 (contract Mock 3).\nrow: {beta_line:?}\n--- screen ---\n{screen}",
            );
        }
        let alpha_line = line_containing(&screen, "alpha");
        for tok in ["alpha", "$0.5000", "~$0.9060", "150k", "1.5M", "15m00s"] {
            assert!(
                has_token(alpha_line, tok),
                "grouped-by-repo, the 'alpha' row must contain token {tok:?} \
                 (contract Mock 3).\nrow: {alpha_line:?}\n--- screen ---\n{screen}",
            );
        }
    }
}
