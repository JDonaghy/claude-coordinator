//! coord-tui library — backend-neutral coordinator dashboard.
//!
//! Exposes [`CoordApp`], which implements [`quadraui::AppLogic`] using
//! only the backend-neutral trait surface. Thin shims in `src/main.rs`
//! (TUI) and `src/bin/gtk.rs` (GTK) wire it to the appropriate runner.

mod app;
pub use app::CoordApp;
