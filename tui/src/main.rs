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

    // Stash the panic message so `catch_unwind` below can retrieve it after
    // the terminal has been restored.  `OnceLock` is panic-safe (no mutex
    // that could deadlock inside the hook).
    static PANIC_MSG: std::sync::OnceLock<String> = std::sync::OnceLock::new();

    // Persist any panic to ~/.coord/coord-tui-panic.log before the shell
    // restores the terminal and the message scrolls offscreen.
    //
    // IMPORTANT: we do NOT chain to the Rust default hook here.  The default
    // hook writes directly to stderr, which is inside the alternate-screen
    // buffer while the TUI is running.  That output is invisible after
    // terminal teardown — and it corrupts the TUI display if the panic fires
    // mid-render before teardown.  Instead we log to a file (always
    // readable) and print a clean one-liner to stderr AFTER `catch_unwind`
    // returns (by which point quadraui has already restored the terminal).
    std::panic::set_hook(Box::new(|info| {
        // Stash the one-line summary for the post-restore message.
        let _ = PANIC_MSG.set(info.to_string());

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
        // No default_hook call — see comment above.
    }));

    // Wrap the TUI run loop in `catch_unwind` so that a panic that escapes
    // quadraui's internal recovery (e.g. during startup or shutdown) still
    // lets quadraui's Drop handlers restore the terminal before we print the
    // post-mortem.  Without this wrapper the process would abort (or the
    // `panic = "abort"` profile would kill it) and leave the terminal in raw
    // mode.
    //
    // `AssertUnwindSafe` is safe here: we immediately exit the process on the
    // Err branch; we never resume normal execution with a potentially
    // inconsistent CoordApp.
    let run_result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        quadraui::tui::shell_runner::run_with_shell(CoordApp::new(), CoordApp::shell_config());
    }));

    if run_result.is_err() {
        // At this point the terminal has been restored by quadraui's Drop
        // handler, so a plain `eprintln!` is safe and visible.
        let summary = PANIC_MSG
            .get()
            .map(String::as_str)
            .unwrap_or("unknown panic");
        eprintln!(
            "\ncoord-tui panicked: {}\n\nFull details in ~/.coord/coord-tui-panic.log\n",
            summary
        );
        std::process::exit(101);
    }
}
