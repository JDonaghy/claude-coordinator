//! coord-tui — GTK binary.
//!
//! Thin shim: wires [`coord_tui::CoordApp`] to the `quadraui::gtk`
//! shell runner. All app logic is backend-neutral and lives in `CoordApp`.
//!
//! The app-id defaults to `"org.quadraui.app"` (the runner's built-in
//! default) until quadraui #234 lands and adds a builder API for
//! custom app-ids and window titles.
use coord_tui::CoordApp;

fn main() {
    quadraui::gtk::shell_runner::run_with_shell(CoordApp::new(), CoordApp::shell_config());
}
