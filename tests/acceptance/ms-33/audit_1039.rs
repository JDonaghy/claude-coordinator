// Sealed acceptance slice for **issue #1039** — "TUI Audit panel:
// SidebarView::Audit list + dedicated /audit fetch" — milestone ms-33
// (tracking issue #1041, Audit Trail epic).
//
// Authored independently from `tests/acceptance/ms-33/contract.md` (Gate A),
// with **zero** worker/implementation context. Drives the whole app through
// the real `event → handle → render` path via quadraui's `TuiDriver` against
// ratatui's headless `TestBackend`, exactly like the in-crate `#[cfg(test)]`
// suite (docs/ORACLE_LOOP.md, coord-tui `tui-tuidriver` driver).
//
// This file is `include!`d at crate root by `tui/tests/acceptance.rs` (the
// #1042 seam target). It is compiled only under `--features test-support`.
// It is SEALED: the worker implementing #1039 may run it
// (`coord acceptance run --issue 1039`) but may not read or edit it.
//
// ── Scope note (why this slice is deliberately narrow) ────────────────────
// The **populated-list** (contract §4a), **entry-detail pane** (§4c) and
// **count / recent-badge** (§3) behaviours require *seeding audit entries*
// into the app with no live daemon. Contract §5 states the cache field name
// is "TBD by implementor", and today `BoardData`'s fields are `pub(crate)`
// with no `Deserialize`, so an **external** integration-test crate has no
// stable public seam to seed audit rows. Those assertions are therefore
// captured as a documented TODO block at the bottom of this file rather than
// guessed — see `tests/acceptance/ms-33` summary. The tests below cover the
// part of the #1039 contract that needs **no seeding**: panel registration
// (§1/§2), sidebar header (§3), empty state (§4b) and status-bar hints (§7).
// They compile against today's public seam and are genuinely RED (they run
// and fail on assertions) until #1039 lands.

mod audit_1039 {
    use coord_tui::fixtures::{make_test_app, BoardData};
    use coord_tui::CoordApp;
    use quadraui::tui::testing::{driver_with_shell, TuiDriver};

    /// Build the app on an empty board and hand back a driver on a
    /// 120×40 grid — the fixture surface every contract mock declares
    /// (`driver_with_shell(app, CoordApp::shell_config(), 120, 40)`).
    fn empty_audit_driver() -> TuiDriver<impl quadraui::AppLogic> {
        let app = make_test_app(BoardData::default());
        driver_with_shell(app, CoordApp::shell_config(), 120, 40)
    }

    /// Activate the Audit panel by clicking its activity-bar icon, then
    /// repaint. Fails loudly (RED) if the `§` icon isn't rendered yet — i.e.
    /// `panel:audit` / `SidebarView::Audit` has not been registered. This is
    /// the pre-implementation failure mode for every audit-view assertion.
    fn nav_to_audit<A: quadraui::AppLogic>(driver: &mut TuiDriver<A>) {
        let (x, y) = driver.find("§").expect(
            "contract §1/§2: activity bar must render the '§' audit icon so \
             the Audit panel can be activated — not found, meaning \
             panel:audit / SidebarView::Audit is not registered yet (#1039)",
        );
        assert!(
            x < 3.0,
            "contract §2: the '§' audit icon must live in the activity-bar \
             columns 0–2 (x < 3.0); found x = {x}",
        );
        driver.click(x, y);
        driver.render();
    }

    /// Contract §1 (panel registration) + §2 (activity-bar rendered text):
    /// the `§` audit icon must appear in the activity bar (columns 0–2).
    #[test]
    fn activity_bar_shows_section_icon() {
        let driver = empty_audit_driver();
        let pos = driver.find("§");
        assert!(
            pos.is_some(),
            "contract §1/§2: the activity bar must render the '§' audit panel \
             icon once panel:audit is registered in shell_config(); not found",
        );
        let (x, _y) = pos.unwrap();
        assert!(
            x < 3.0,
            "contract §2: '§' must be within activity-bar columns 0–2 \
             (x < 3.0); found x = {x}",
        );
    }

    /// Contract §3: with the Audit panel active, the sidebar header (rendered
    /// by `ShellConfig.with_status_bar()` from `title: \"AUDIT\"`) shows the
    /// padded title `\" AUDIT \"`.
    #[test]
    fn sidebar_header_shows_title() {
        let mut driver = empty_audit_driver();
        nav_to_audit(&mut driver);
        assert!(
            driver.screen_contains(" AUDIT "),
            "contract §3: the sidebar header must show \" AUDIT \" when \
             SidebarView::Audit is active.\n--- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §4b (empty-state edge case): on an empty board — no audit
    /// entries, fetch not (or never) completed — the main content area shows
    /// `\"No audit events yet.\"`.
    #[test]
    fn empty_state_message() {
        let mut driver = empty_audit_driver();
        nav_to_audit(&mut driver);
        assert!(
            driver.screen_contains("No audit events yet."),
            "contract §4b: an empty Audit panel must render \
             \"No audit events yet.\" in the main content area.\n\
             --- screen ---\n{}",
            driver.screen(),
        );
    }

    /// Contract §7: status-bar hints shown when the Audit view is active and
    /// no detail pane is open. `Enter=detail` and `r=refresh` are audit-
    /// specific; `j/k=nav` and `q=quit` complete the required set.
    #[test]
    fn status_bar_hints_list_mode() {
        let mut driver = empty_audit_driver();
        nav_to_audit(&mut driver);
        let screen = driver.screen();
        for needle in ["j/k=nav", "Enter=detail", "r=refresh", "q=quit"] {
            assert!(
                screen.contains(needle),
                "contract §7: Audit list-mode status bar must contain {needle:?}\
                 .\n--- screen ---\n{screen}",
            );
        }
    }

    // ───────────────────────────────────────────────────────────────────────
    // DEFERRED — needs a public audit-seeding fixture (contract §3 count /
    // recent badge, §4a populated list, §4c entry-detail pane).
    //
    // TODO(test-author): contract §5 leaves the audit-cache field name "TBD by
    // implementor", and today `BoardData` (fields `pub(crate)`, no
    // `Deserialize`) offers no stable public seam for an *external* test crate
    // to seed audit rows without a live daemon. Once the #1039 worker exposes
    // such a seam (per its briefing: an `audit`-shaped field on
    // `BoardData`/`CoordApp` reachable from `make_test_app`, e.g. a `pub`
    // field + `pub` entry type, or a `make_app_with_audit_json(&str)` helper
    // matching the /audit wire shape in contract §6), a JIT extension of this
    // suite should add, seeding the 6-entry mock from
    // `mocks/audit-panel-populated.screen`:
    //
    //   * populated_list_shows_categories   — screen_contains("dispatch"),
    //       "test", "review", "merge"                              (§4a)
    //   * populated_list_shows_actor        — screen_contains("coordinator") (§4a)
    //   * populated_list_rows_are_relative_time — screen_contains("ago")   (§4a)
    //   * populated_list_newest_first       — highest ts row above lower    (§4a)
    //   * sidebar_count_line                — screen_contains("42 entries") (§3)
    //   * sidebar_recent_badge              — screen_contains("7 recent")   (§3)
    //   * entry_detail_pane_on_enter        — after nav + select + Enter,
    //       screen_contains("Entry Detail","ts:","category:","event_type:",
    //       "actor:","summary:")                                          (§4c)
    //   * entry_detail_esc_closes           — Esc returns to list-only view (§4c)
    //   * detail_mode_status_hint           — screen_contains("Esc=close detail")
    //                                                                       (§7)
    //
    // These are intentionally NOT authored here (not manifest-mapped) rather
    // than guessed against an unstable/absent seam — see the ms-33 summary.
    // ───────────────────────────────────────────────────────────────────────
}
