//! coord-tui library — backend-neutral coordinator dashboard.
//!
//! Exposes [`CoordApp`], which implements [`quadraui::ShellApp`] using
//! only the backend-neutral trait surface. Thin shims in `src/main.rs`
//! (TUI) and `src/bin/gtk.rs` (GTK) wire it to the appropriate shell runner.

mod app;
pub use app::CoordApp;
