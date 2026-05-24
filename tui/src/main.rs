//! coord-tui — TUI binary.
//!
//! Thin shim: wires [`coord_tui::CoordApp`] to `quadraui::tui::shell_runner`.
//! All app logic lives in `CoordApp`; quadraui owns terminal
//! setup/teardown, the AppShell chrome, and the crossterm event loop.

use coord_tui::CoordApp;

fn main() {
    // Persist any panic to ~/.coord/coord-tui-panic.log before the shell
    // restores the terminal and the message scrolls offscreen. The previous
    // hook (Rust's default) writes to stderr inside the alternate screen,
    // which is invisible after teardown. Keep the default hook running too so
    // users with stderr captured still see the message.
    let default_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        if let Some(home) = std::env::var_os("HOME") {
            let log_dir = std::path::Path::new(&home).join(".coord");
            let _ = std::fs::create_dir_all(&log_dir);
            let log_path = log_dir.join("coord-tui-panic.log");
            if let Ok(mut f) = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&log_path)
            {
                use std::io::Write;
                let ts = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs())
                    .unwrap_or(0);
                let _ = writeln!(
                    f,
                    "\n--- unix_ts={} ---\n{}\n{}",
                    ts,
                    info,
                    std::backtrace::Backtrace::force_capture()
                );
            }
        }
        default_hook(info);
    }));

    quadraui::tui::shell_runner::run_with_shell(CoordApp::new(), CoordApp::shell_config());
}
