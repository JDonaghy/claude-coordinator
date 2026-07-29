// Sealed acceptance slice for **issue #1124** — "Plans panel: wire in `?`
// help overlay + command palette" — milestone ms-38 (tracking issue #1120,
// "Plans panel -> rich client" epic).
//
// Authored independently from `tests/acceptance/ms-38/contract.md` (Gate A),
// with **zero** worker/implementation context. Drives the whole app through
// the real `event → handle → render` path via quadraui's `TuiDriver` against
// ratatui's headless `TestBackend` (docs/ORACLE_LOOP.md, coord-tui
// `tui-tuidriver` driver).
//
// This file is `include!`d at crate root by `tui/tests/acceptance.rs` (the
// #1042 seam target). It is compiled only under `--features test-support`.
// It is SEALED: the worker implementing #1124 may run it
// (`coord acceptance run --issue 1124`) but may not read or edit it.
//
// Scope: contract §5 (CC-4) only — §5a–§5i. The sibling children's surfaces
// (§3 CC-2 detail pane / #1122, §4 CC-3 right-click menus / #1123) are NOT
// covered here; their slices are authored separately against the same
// contract and append to the shared `manifest.yml`.
//
// ── Why every assertion runs on an EMPTY plan roster ──────────────────────
// Contract §6 asks for a `BoardData` carrying `plan_roster` entries, but
// `BoardData`'s fields are `pub(crate)` (see the note above the struct in
// `tui/src/app/types.rs`: "external callers only ever need
// `BoardData::default()`"), and no `make_app_with_plan_roster*` seam is
// pinned by the contract or exposed by `coord_tui::fixtures`. An *external*
// integration-test crate therefore cannot seed plan rows — the same wall the
// ms-33 slice documented for audit entries.
//
// That is not a problem for THIS slice: §5a and §5e both key the trigger off
// `active_view == SidebarView::Plans`, not off a selected row, and every
// required string in §5b–§5g is *static chrome* (cheatsheet text, chip
// legend, action labels) rather than roster-derived data. So the whole CC-4
// surface is reachable with `BoardData::default()`.
//
// ── The exact state these tests run in (verified, not assumed) ────────────
// Because `BoardData::default()` also leaves `plan_roster_supported` false,
// clicking `◆` lands on a Plans panel whose SIDEBAR renders normally
// (" PLANS " header, "All repos" root) but whose MAIN AREA renders the
// unavailable placeholder:
//
//     Plans unavailable — not receiving plan-roster data. Requires a `coord serve`…
//
// That is the observed pre-implementation screen, captured by running this
// file. It is not a defect in these tests — it is the only Plans state an
// external acceptance crate can reach today.
//
// TODO(test-author): the contract does not state whether `?` / `/` must work
// when the roster is empty / unsupported. §5a and §5e are written purely in
// terms of `active_view == SidebarView::Plans`, with no roster or selection
// precondition, so these tests take that at face value and open both
// overlays in the state above. **Implementor: this means the help overlay
// and the palette must render even while the main area shows the
// "Plans unavailable" placeholder** — which is defensible on its own terms
// (a cheatsheet is most useful precisely when the panel looks broken), but
// it is an inference from §5a/§5e's wording rather than something the
// contract says outright. If you believe the overlays should instead be
// gated on live roster data, that is a contract-amendment conversation
// (§5a/§5e would need to grow a precondition) — do not silently special-case
// these tests, and do not "fix" them by gating the overlay.

mod plans_help_1124 {
    use coord_tui::fixtures::{make_test_app, BoardData};
    use coord_tui::CoordApp;
    use quadraui::tui::testing::{driver_with_shell, TuiDriver};
    use quadraui::NamedKey;

    /// Build the app on an empty board and hand back a driver on the 120×40
    /// grid every ms-38 mock declares (contract §7:
    /// `driver_with_shell(app, CoordApp::shell_config(), 120, 40)`).
    fn plans_driver() -> TuiDriver<impl quadraui::AppLogic> {
        let app = make_test_app(BoardData::default());
        driver_with_shell(app, CoordApp::shell_config(), 120, 40)
    }

    /// Activate the Plans panel by clicking its activity-bar icon, then
    /// repaint. Contract §1 pins `PanelDefinition.icon == "◆"` for
    /// `panel:plans`; CC-1 (#1121) already shipped this, so a failure here
    /// means the *baseline* regressed, not that #1124 is unimplemented.
    fn nav_to_plans<A: quadraui::AppLogic>(driver: &mut TuiDriver<A>) {
        let (x, y) = driver.find("◆").expect(
            "contract §1: the activity bar must render the '◆' Plans panel \
             icon so the Plans panel can be activated — not found. This is \
             the CC-1 (#1121) baseline, which the contract records as \
             already shipped",
        );
        assert!(
            x < 3.0,
            "contract §1: the '◆' Plans icon must live in the activity-bar \
             columns 0–2 (x < 3.0); found x = {x}. A match further right \
             means find() latched onto a '◆' in the content area instead of \
             the activity bar",
        );
        driver.click(x, y);
        driver.render();
    }

    /// Open the Plans help overlay: activate Plans, press `?` (contract §5a),
    /// repaint.
    fn open_help_overlay<A: quadraui::AppLogic>(driver: &mut TuiDriver<A>) {
        nav_to_plans(driver);
        driver.type_char('?');
        driver.render();
    }

    /// Open the Plans command palette: activate Plans, press `/` (contract
    /// §5e — the trigger is locked to `/`; `Ctrl+P` is permitted only as an
    /// *additional* alias, so `/` must work on its own), repaint.
    fn open_palette<A: quadraui::AppLogic>(driver: &mut TuiDriver<A>) {
        nav_to_plans(driver);
        driver.type_char('/');
        driver.render();
    }

    /// Assert every needle in `needles` is on screen, reporting **all**
    /// missing ones at once (not just the first) so the implementor sees the
    /// whole gap in a single run.
    fn assert_all_present<A: quadraui::AppLogic>(
        driver: &TuiDriver<A>,
        needles: &[&str],
        clause: &str,
    ) {
        let missing: Vec<&str> = needles
            .iter()
            .copied()
            .filter(|n| !driver.screen_contains(n))
            .collect();
        assert!(
            missing.is_empty(),
            "{clause}: {} of {} required string(s) missing from the screen: \
             {missing:?}\n--- screen ---\n{}",
            missing.len(),
            needles.len(),
            driver.screen(),
        );
    }

    // ── §5a / §5b — help overlay trigger + title ──────────────────────────

    /// Contract §5a (trigger) + §5b (title): pressing `?` while the Plans
    /// panel is active opens a cheatsheet modal titled `"Plans — Help"`.
    ///
    /// Note the title uses an em dash (U+2014) with spaces either side,
    /// exactly as §5b and `mocks/plans-help-overlay.screen` spell it.
    #[test]
    fn help_overlay_opens_on_question_mark() {
        let mut driver = plans_driver();
        open_help_overlay(&mut driver);
        assert!(
            driver.screen_contains("Plans — Help"),
            "contract §5a/§5b: pressing '?' with the Plans panel active must \
             open a cheatsheet modal whose title is \"Plans — Help\".\n\
             --- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §5a (close): Esc closes the help overlay.
    ///
    /// The open-state is asserted **first** on purpose: without it this test
    /// would pass vacuously against an implementation that never renders the
    /// overlay at all (the title is absent before *and* after Esc), which
    /// would make it a false green rather than a RED test.
    #[test]
    fn help_overlay_closes_on_esc() {
        let mut driver = plans_driver();
        open_help_overlay(&mut driver);
        assert!(
            driver.screen_contains("Plans — Help"),
            "contract §5a/§5b: precondition — '?' must open the help overlay \
             before this test can verify Esc closes it.\n--- screen ---\n{}",
            driver.screen(),
        );
        driver.press_named(NamedKey::Escape);
        driver.render();
        assert!(
            !driver.screen_contains("Plans — Help"),
            "contract §5a: Esc must close the help overlay — the \
             \"Plans — Help\" title is still on screen.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ── §5c — help overlay key entries ────────────────────────────────────

    /// Contract §5c (Navigation column): the cheatsheet lists the
    /// right-click / `Enter` / `Esc` meanings.
    ///
    /// Each needle is a phrase that occurs **only inside the overlay** — per
    /// §5c's own rationale, the bare key characters would be satisfied by the
    /// Plans status bar alone, with no overlay open.
    #[test]
    fn help_overlay_navigation_entries() {
        let mut driver = plans_driver();
        open_help_overlay(&mut driver);
        assert_all_present(
            &driver,
            &["open context menu", "open detail pane", "close / back"],
            "contract §5c: the help overlay's navigation entries",
        );
    }

    /// Contract §5c (Actions column): the cheatsheet lists the `?`, `/`, `c`,
    /// `C` and `u` action meanings.
    ///
    /// `"quick capture plan"` and `"toggle untracked milestones"` are
    /// deliberately longer than the base status bar's `capture plan` /
    /// `toggle untracked`, and lowercase where the palette's own entries are
    /// capitalised (§5g) — `screen_contains` is case-sensitive, so these
    /// cannot be satisfied by the status bar or by the palette.
    #[test]
    fn help_overlay_action_entries() {
        let mut driver = plans_driver();
        open_help_overlay(&mut driver);
        assert_all_present(
            &driver,
            &[
                "this help overlay",
                "command palette",
                "quick capture plan",
                "guided chat (new plan)",
                "toggle untracked milestones",
            ],
            "contract §5c: the help overlay's action entries",
        );
    }

    // ── §5d — health chip legend ──────────────────────────────────────────

    /// Contract §5d: the overlay includes a health-chip legend naming all
    /// four chip states.
    #[test]
    fn help_overlay_health_chip_legend() {
        let mut driver = plans_driver();
        open_help_overlay(&mut driver);
        assert_all_present(
            &driver,
            &["ready_waiting", "stalled", "chat_pending", "no_work_order"],
            "contract §5d: the help overlay's health-chip legend",
        );
    }

    // ── §5e / §5f — palette trigger + title ───────────────────────────────

    /// Contract §5e (trigger) + §5f (title): pressing `/` while the Plans
    /// panel is active opens the command palette.
    ///
    /// **Both** `"command palette"` and `"Plans actions"` are required. Since
    /// the 2026-07-28 amendment the help overlay also contains the string
    /// `"command palette"` (§5c requires it there, so `/` is discoverable),
    /// so that string alone no longer proves the *palette* is what opened —
    /// a run that rendered the help overlay would satisfy it. `"Plans
    /// actions"` is the §5f section header and appears in no other screen
    /// state, closing that gap.
    #[test]
    fn palette_opens_on_slash() {
        let mut driver = plans_driver();
        open_palette(&mut driver);
        assert_all_present(
            &driver,
            &["command palette", "Plans actions"],
            "contract §5e/§5f: pressing '/' with the Plans panel active must \
             open the command palette",
        );
    }

    /// Contract §5e (close): Esc closes the command palette.
    ///
    /// As with the help-overlay close test, the open-state is asserted first
    /// so this cannot pass vacuously against an implementation that never
    /// opens the palette.
    #[test]
    fn palette_closes_on_esc() {
        let mut driver = plans_driver();
        open_palette(&mut driver);
        assert!(
            driver.screen_contains("Plans actions"),
            "contract §5e/§5f: precondition — '/' must open the command \
             palette before this test can verify Esc closes it.\n\
             --- screen ---\n{}",
            driver.screen(),
        );
        driver.press_named(NamedKey::Escape);
        driver.render();
        assert!(
            !driver.screen_contains("Plans actions"),
            "contract §5e: Esc must close the command palette — the \
             \"Plans actions\" section header is still on screen.\n\
             --- screen ---\n{}",
            driver.screen(),
        );
    }

    // ── §5g — palette Plans action entries ────────────────────────────────

    /// Contract §5g: while the palette is open and unfiltered, every
    /// registered Plans action label is listed.
    ///
    /// §5g marks three of these as explicitly "Required"; the table lists
    /// eight, and all eight are asserted here — the table says each label
    /// "must appear in the palette while it is open and unfiltered".
    #[test]
    fn palette_lists_plans_actions() {
        let mut driver = plans_driver();
        open_palette(&mut driver);
        assert_all_present(
            &driver,
            &[
                "Dispatch milestone",
                "Open milestone chat",
                "Quick capture plan",
                "Guided chat (new plan)",
                "View order / DAG",
                "Edit milestone…",
                "Add issue to milestone…",
                "Toggle untracked milestones",
            ],
            "contract §5g: the palette's Plans action entries",
        );
    }

    // ── §5h — palette search filtering ────────────────────────────────────

    /// Contract §5h: typing a search string narrows the palette to matching
    /// entries — `"Dispatch milestone"` survives the query `"dispatch"` while
    /// non-matching entries drop out of the display.
    ///
    /// The query is typed lowercase and the surviving label is capitalised,
    /// so §5h's own "still true" requirement pins the match as
    /// **case-insensitive**.
    ///
    /// The two negative needles are chosen so they cannot be satisfied by
    /// chrome outside the palette: `"Quick capture plan"` is capital-Q where
    /// the status bar's hint is lowercase `c=capture plan`, and `"Toggle
    /// untracked milestones"` is capital-T where the help overlay's wording
    /// (§5c) is lowercase.
    #[test]
    fn palette_search_filters_entries() {
        let mut driver = plans_driver();
        open_palette(&mut driver);
        assert!(
            driver.screen_contains("Quick capture plan"),
            "contract §5g/§5h: precondition — the unfiltered palette must \
             list \"Quick capture plan\" before this test can verify that \
             searching filters it out.\n--- screen ---\n{}",
            driver.screen(),
        );

        for c in "dispatch".chars() {
            driver.type_char(c);
        }
        driver.render();

        assert!(
            driver.screen_contains("Dispatch milestone"),
            "contract §5h: after typing \"dispatch\", the matching entry \
             \"Dispatch milestone\" must still be listed.\n\
             --- screen ---\n{}",
            driver.screen(),
        );
        let still_shown: Vec<&str> = ["Quick capture plan", "Toggle untracked milestones"]
            .into_iter()
            .filter(|n| driver.screen_contains(n))
            .collect();
        assert!(
            still_shown.is_empty(),
            "contract §5h: after typing \"dispatch\", entries not matching \
             the query must be absent from the palette display; still \
             shown: {still_shown:?}\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ── §5i — status bar while an overlay is open ─────────────────────────

    /// Contract §5i: `"Esc=close"` is shown while the **help overlay** is
    /// open.
    #[test]
    fn help_overlay_status_bar_shows_esc_close() {
        let mut driver = plans_driver();
        open_help_overlay(&mut driver);
        assert!(
            driver.screen_contains("Esc=close"),
            "contract §5i: with the help overlay open, the status bar must \
             show \"Esc=close\".\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §5i: `"Esc=close"` is shown while the **command palette** is
    /// open.
    #[test]
    fn palette_status_bar_shows_esc_close() {
        let mut driver = plans_driver();
        open_palette(&mut driver);
        assert!(
            driver.screen_contains("Esc=close"),
            "contract §5i: with the command palette open, the status bar \
             must show \"Esc=close\".\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    // ───────────────────────────────────────────────────────────────────────
    // NOT AUTHORED — deliberately, with reasons.
    //
    // TODO(test-author): §5c lists `r` (refresh) and `q` (quit) in the
    // cheatsheet, but §5c's own note drops them from the required set —
    // the overlay's wording for them ("refresh" / "quit") is not
    // distinguishable from the base status bar's, so an assertion on either
    // would pass with no overlay rendered. They stay in
    // `mocks/plans-help-overlay.screen` but are not load-bearing here.
    //
    // TODO(test-author): §5e permits `Ctrl+P` as an *additional* palette
    // alias but does not require it, so no test asserts it. If a future
    // contract round makes the alias mandatory, add a
    // `palette_opens_on_ctrl_p` using `driver.ctrl_char('p')`.
    //
    // TODO(test-author): §5g pins each palette entry's *label* and its bound
    // action id, but the mock also shows a per-entry **description** column
    // ("dispatch selected plan's ready frontier", …). The contract's §5g
    // table has no description column and the issue body only says
    // "every action with a description", so the exact description strings
    // are unpinned and are not asserted — only the labels are.
    // ───────────────────────────────────────────────────────────────────────
}
