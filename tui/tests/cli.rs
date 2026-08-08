//! Black-box CLI test for coord-tui's `--version`/`-V` flag.
//!
//! #1239 (PKG-3) fix iteration: the unit tests on `wants_version`/
//! `version_string` in `src/main.rs` only exercise those extracted helper
//! functions directly — they never invoke the compiled binary, so they
//! can't catch a break in the actual argv -> stdout wiring in `main()`
//! (e.g. an accidental early return before the `println!`, a typo in the
//! flag string, or `CARGO_PKG_VERSION` failing to propagate at build
//! time). CLAUDE.md's testing policy requires a black-box test for any PR
//! that ships new user-visible CLI behavior, and `--version`/`-V` is
//! exactly that.
//!
//! The `TuiDriver` harness (`tests/acceptance.rs` and friends) doesn't fit
//! here: `--version` returns before any terminal/`ShellApp` setup runs, so
//! there's nothing for that harness to attach to. Instead this drives the
//! *actual compiled binary* as a subprocess via
//! `Command::new(env!("CARGO_BIN_EXE_coord-tui"))` and asserts on its real
//! stdout — the same wiring release-tui.yml's "Verify --version reports
//! the tag version" step checks, just running on every `cargo test`
//! instead of only at tag-push time.
//!
//! No `test-support` feature gate needed: this only spawns the plain
//! `coord-tui` binary, which is always built.

use std::process::Command;

/// Runs `coord-tui <arg>` and returns its stdout, asserting a clean exit.
fn run_coord_tui(arg: &str) -> String {
    let output = Command::new(env!("CARGO_BIN_EXE_coord-tui"))
        .arg(arg)
        .output()
        .expect("failed to spawn the compiled coord-tui binary");
    assert!(
        output.status.success(),
        "coord-tui {arg} exited non-zero: status={:?} stderr={}",
        output.status,
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).expect("coord-tui stdout was not valid UTF-8")
}

#[test]
fn long_flag_prints_version_to_real_stdout() {
    let stdout = run_coord_tui("--version");
    assert_eq!(
        stdout.trim(),
        format!("coord-tui {}", env!("CARGO_PKG_VERSION"))
    );
}

#[test]
fn short_flag_prints_version_to_real_stdout() {
    let stdout = run_coord_tui("-V");
    assert_eq!(
        stdout.trim(),
        format!("coord-tui {}", env!("CARGO_PKG_VERSION"))
    );
}
