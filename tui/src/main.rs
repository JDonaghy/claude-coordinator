//! coord-tui — TUI binary.
//!
//! Thin shim: wires [`coord_tui::CoordApp`] to `quadraui::tui::run`.
//! All app logic lives in `CoordApp`; quadraui owns terminal
//! setup/teardown and the crossterm event loop.

use coord_tui::CoordApp;

fn main() -> std::io::Result<()> {
    quadraui::tui::run(CoordApp::new())
}
