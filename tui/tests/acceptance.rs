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

use coord_tui::fixtures::{make_test_app, BoardData};

#[test]
fn make_test_app_builds_from_board_data_with_no_live_daemon() {
    // Constructing the app — no daemon, no I/O, no panic — is the
    // assertion: it proves `app::fixtures` is reachable from an external
    // integration-test crate under the `test-support` feature.
    let app = make_test_app(BoardData::default());
    let _ = app;
}
