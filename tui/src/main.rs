//! coord-tui — TUI binary.
//!
//! Thin shim: wires [`coord_tui::CoordApp`] to `quadraui::tui::shell_runner`.
//! All app logic lives in `CoordApp`; quadraui owns terminal
//! setup/teardown, the AppShell chrome, and the crossterm event loop.

use coord_tui::CoordApp;

fn main() {
    // Force non-interactive mode on every subprocess the TUI (or any tool
    // it spawns) launches.  Without these, an SSH passphrase or HTTPS
    // credential prompt from a child git/ssh process can grab the TTY,
    // corrupting the TUI display or hanging silently waiting for input
    // that never arrives.
    //
    // - GIT_TERMINAL_PROMPT=0       — git itself never asks for credentials
    // - GIT_SSH_COMMAND BatchMode   — ssh fails fast instead of prompting
    //   (10 s ConnectTimeout keeps a misconfigured remote from hanging)
    // - SSH_ASKPASS=/bin/false      — any GUI password helper fails too
    //
    // The user can still load their key into ssh-agent before launching
    // the TUI for normal workflows; these env vars just guarantee the
    // failure mode is "fast and visible" instead of "frozen TTY".
    // SAFETY: set_var is `unsafe` in recent stdlib — single-threaded
    // setup before any work begins, so no data race.
    unsafe {
        std::env::set_var("GIT_TERMINAL_PROMPT", "0");
        std::env::set_var(
            "GIT_SSH_COMMAND",
            "ssh -o BatchMode=yes -o ConnectTimeout=10",
        );
        std::env::set_var("SSH_ASKPASS", "/bin/false");
    }

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
