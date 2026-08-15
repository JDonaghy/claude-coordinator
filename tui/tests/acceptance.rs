//! #1042 seam smoke test.
//!
//! Proves that, with the `test-support` feature enabled, an *external*
//! integration-test crate can build a [`coord_tui::CoordApp`] from
//! in-memory `BoardData` — no live daemon — exactly the way the in-crate
//! `#[cfg(test)]` suite does via `app::fixtures`. This is the harness seam
//! the oracle-loop's `tui-tuidriver` driver (docs/ORACLE_LOOP.md) assumes
//! exists at `tui/tests/acceptance.rs`.
//!
//! This is **not** the real Gate-A acceptance suite for #1039/#1040 — that
//! is independently authored later by #931's `test-author`, from #1041's
//! Gate-A contract. This file only proves the seam works.
//!
//! Run with:
//!   cargo test --test acceptance --features test-support
//! (the sealed-suite invocation adds `RUSTC_BOOTSTRAP=1 ... -- -Z
//! unstable-options --format json` for libtest JSON-lines output; see #1042
//! deliverable 4 / `coordinator.yml` `acceptance.drivers`.)
#![cfg(feature = "test-support")]

use coord_tui::fixtures::{make_app_with_board_json, make_test_app, BoardData};

#[test]
fn make_test_app_builds_from_board_data_with_no_live_daemon() {
    // Constructing the app — no daemon, no I/O, no panic — is the
    // assertion: it proves `app::fixtures` is reachable from an external
    // integration-test crate under the `test-support` feature.
    let app = make_test_app(BoardData::default());
    let _ = app;
}

/// #2281 seam smoke test.
///
/// Proves the gap the issue describes is actually closed: before this seam,
/// `BoardData`'s fields (and `OpenIssue`) were `pub(crate)`, so this crate
/// could build only an empty board (`BoardData::default()`) — no tree rows
/// in the Board sidebar, nothing feeding the Pipeline tracked-issue list.
/// `make_app_with_board_json` seeds three open issues across two repos from
/// a raw JSON object shaped like the daemon's `/board` payload, and both
/// panels must render real rows from it.
#[test]
fn make_app_with_board_json_renders_seeded_issues_in_board_and_pipeline() {
    use coord_tui::CoordApp;
    use quadraui::tui::testing::driver_with_shell;

    const BOARD_JSON: &str = r#"{
      "issues": [
        {"repo_name": "repo-a", "number": 2266, "title": "Alpha issue", "state": "open", "labels": ["coord"]},
        {"repo_name": "repo-a", "number": 2267, "title": "Beta issue", "state": "open", "labels": ["coord"]},
        {"repo_name": "repo-b", "number": 2268, "title": "Gamma issue", "state": "open", "labels": ["coord"]}
      ],
      "board_meta": {
        "pipeline_repos": "{\"repo-a\": \"acme/repo-a\", \"repo-b\": \"acme/repo-b\"}",
        "pipeline_default_gates": "[\"review\", \"merge\"]"
      }
    }"#;

    let app = make_app_with_board_json(BOARD_JSON);
    let mut driver = driver_with_shell(app, CoordApp::shell_config(), 120, 40);
    driver.render();

    // SidebarView::Board is the app's default active view, so the seeded
    // repo groups are already on screen — but each repo's "No milestone"
    // sub-group starts collapsed (no in-flight assignment to force it open,
    // #857's default), so the issue rows themselves need one click each to
    // reveal. Target by the group's issue count ("(2)" / "(1)") since both
    // repos otherwise render an identical "No milestone" header.
    let (x, y) = driver
        .find("No milestone (2)")
        .expect("repo-a's collapsed \"No milestone\" group header must be on screen");
    driver.click(x, y);
    driver.render();
    assert!(
        driver.screen_contains("#2266"),
        "Board sidebar must render a row for seeded issue #2266 — screen:\n{}",
        driver.screen(),
    );
    assert!(
        driver.screen_contains("#2267"),
        "Board sidebar must render a row for seeded issue #2267 — screen:\n{}",
        driver.screen(),
    );

    let (x, y) = driver
        .find("No milestone (1)")
        .expect("repo-b's collapsed \"No milestone\" group header must be on screen");
    driver.click(x, y);
    driver.render();
    assert!(
        driver.screen_contains("#2268"),
        "Board sidebar must render a row for seeded issue #2268 — screen:\n{}",
        driver.screen(),
    );

    // Switch to the Pipeline panel (activity-bar icon "▶") and confirm the
    // same seed feeds its tracked-issue list too.
    let (x, y) = driver
        .find("▶")
        .expect("activity bar must render the Pipeline icon");
    driver.click(x, y);
    driver.render();
    assert!(
        driver.screen_contains(" PIPELINE "),
        "clicking the Pipeline icon must activate SidebarView::Pipeline — screen:\n{}",
        driver.screen(),
    );
    // All three seeded issues carry the default-tracked "coord" label and no
    // assignment yet, so they land in a single "New (3)" lifecycle section —
    // proof `pipeline_issues_from_cache` picked up all three from the seed.
    assert!(
        driver.screen_contains("New (3)"),
        "Pipeline tracked-issue list must show all 3 seeded issues under New — screen:\n{}",
        driver.screen(),
    );
    // Drill in one more level (same collapsed-by-default "No milestone"
    // sub-group as the Board sidebar) to confirm an actual issue row, not
    // just the count, renders from the seed.
    let (x, y) = driver
        .find("No milestone (2)")
        .expect("repo-a's collapsed \"No milestone\" group header must be on screen");
    driver.click(x, y);
    driver.render();
    assert!(
        driver.screen_contains("#2266"),
        "Pipeline tracked-issue list must render seeded issue #2266 — screen:\n{}",
        driver.screen(),
    );
}

/// #2281: malformed JSON must not panic — it degrades to the same
/// empty-board render every other JSON seam in `app::fixtures` falls back
/// to (`make_app_with_audit_json`, `make_app_with_drive_queue`, ...).
#[test]
fn make_app_with_board_json_malformed_json_is_a_silent_no_op() {
    let app = make_app_with_board_json("not valid json");
    let _ = app;
}

// ── Sealed oracle suite (docs/ORACLE_LOOP.md) — DO NOT REMOVE ─────────────
// Wires each milestone's independently-authored acceptance slice under
// `tests/acceptance/ms-NN/` into this `--test acceptance` target so the
// configured `tui-tuidriver` driver command runs them. The slice files hold
// the assertions; this file only pastes them in at crate root. Paths are
// relative to this file (`tui/tests/`), so `../../` is the repo root.
include!("../../tests/acceptance/ms-33/audit_1039.rs");
include!("../../tests/acceptance/ms-38/plans_help_1124.rs");
include!("../../tests/acceptance/ms-38/plans_rightclick_1123.rs");
include!("../../tests/acceptance/ms-38/plans_detail_1122.rs");
