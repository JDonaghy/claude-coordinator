//! coord-tui — TUI binary.
//!
//! Thin shim: wires [`coord_tui::CoordApp`] to `quadraui::tui::shell_runner`.
//! All app logic lives in `CoordApp`; quadraui owns terminal
//! setup/teardown, the AppShell chrome, and the crossterm event loop.

use coord_tui::CoordApp;

fn main() {
    quadraui::tui::shell_runner::run_with_shell(CoordApp::new(), CoordApp::shell_config());
}
