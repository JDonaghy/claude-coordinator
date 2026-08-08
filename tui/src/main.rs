//! coord-tui — TUI binary.
//!
//! Thin shim: wires [`coord_tui::CoordApp`] to `quadraui::tui::shell_runner`.
//! All app logic lives in `CoordApp`; quadraui owns terminal
//! setup/teardown, the AppShell chrome, and the crossterm event loop.

use coord_tui::CoordApp;

/// Returns `true` when `args` contains `--version` or `-V`.
///
/// Extracted from `main` so the flag-matching logic itself is unit
/// testable — `main`'s body (terminal takeover, panic hook, `catch_unwind`)
/// isn't something a `#[test]` can exercise directly.
fn wants_version<I, S>(args: I) -> bool
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    args.into_iter()
        .any(|a| a.as_ref() == "--version" || a.as_ref() == "-V")
}

/// The `--version`/`-V` output, e.g. `coord-tui 0.4.71`.
///
/// #1239 (PKG-3): reads `CARGO_PKG_VERSION`, which cargo populates from
/// `tui/Cargo.toml`'s `[package] version`. The release workflow
/// (`.github/workflows/release-tui.yml`) stamps that field from the pushed
/// `vX.Y.Z` tag before building — the same tag the Python wheel's
/// setuptools-scm version derives from — so `coord-tui --version` and
/// `coord --version` agree on a tagged release build. There is
/// deliberately no separate version literal here to drift out of sync
/// with.
fn version_string() -> String {
    format!("coord-tui {}", env!("CARGO_PKG_VERSION"))
}

fn main() {
    // `--version`/`-V` short-circuits before any of the TUI setup below
    // (env var poking, panic hook install, terminal takeover) — a version
    // check must never touch the terminal or spawn anything.
    if wants_version(std::env::args().skip(1)) {
        println!("{}", version_string());
        return;
    }

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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wants_version_detects_long_flag() {
        assert!(wants_version(["--version"]));
    }

    #[test]
    fn wants_version_detects_short_flag() {
        assert!(wants_version(["-V"]));
    }

    #[test]
    fn wants_version_ignores_unrelated_args() {
        assert!(!wants_version(["--help"]));
        assert!(!wants_version(Vec::<&str>::new()));
    }

    #[test]
    fn wants_version_finds_flag_anywhere_in_argv() {
        // argv[0] (the program path) is already skipped by the caller, but
        // the flag can still appear after other args (e.g. `coord-tui -x
        // --version`) — it shouldn't have to be first.
        assert!(wants_version(["-x", "--version"]));
    }

    #[test]
    fn version_string_matches_cargo_pkg_version() {
        // Cargo populates CARGO_PKG_VERSION from tui/Cargo.toml's [package]
        // version at compile time — see release-tui.yml's version-stamp
        // step, which rewrites that field from the release tag before
        // building. This test guards the format (`coord-tui <version>`),
        // not a hardcoded version number.
        assert_eq!(
            version_string(),
            format!("coord-tui {}", env!("CARGO_PKG_VERSION"))
        );
        assert!(version_string().starts_with("coord-tui "));
    }
}
