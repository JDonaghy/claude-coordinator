//! coord-tui library — backend-neutral coordinator dashboard.
//!
//! Exposes [`CoordApp`], which implements [`quadraui::ShellApp`] using
//! only the backend-neutral trait surface. Thin shims in `src/main.rs`
//! (TUI) and `src/bin/gtk.rs` (GTK) wire it to the appropriate shell runner.

mod app;
mod commands;
pub mod settings;
pub use app::CoordApp;

// #1042: re-export the `test-support`-feature-gated fixtures at the crate
// root so an external integration-test crate (`tui/tests/acceptance.rs`)
// can do `use coord_tui::fixtures::{make_test_app, BoardData};` and build a
// `CoordApp` from in-memory `BoardData` — no live daemon — exactly like the
// in-crate `#[cfg(test)]` suite does. Absent entirely for a normal
// `cargo build`/`cargo test` (the feature is off), so this is not part of
// the crate's default public surface.
#[cfg(any(test, feature = "test-support"))]
pub use app::fixtures;
