//! coord-tui — TUI binary.
//!
//! Thin shim: wires [`coord_tui::CoordApp`] to the `quadraui::tui`
//! runner. All app logic lives in `CoordApp`; the runner owns terminal
//! setup/teardown, the crossterm event loop, and ratatui rasterisers.
use coord_tui::CoordApp;

fn main() -> std::io::Result<()> {
    quadraui::tui::run(CoordApp::new())
}
