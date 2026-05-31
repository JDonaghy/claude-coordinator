use std::io::Read;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

/// Maximum stderr bytes captured per command.  Bounds memory + toast size when
/// a worker is extremely verbose; the head is the most useful bit for diagnosing
/// shell-level failures (`gh: not found`, missing label, auth prompt, etc.).
const STDERR_CAPTURE_BYTES: usize = 2048;

pub struct CommandResult {
    pub label: String,
    pub exit_code: i32,
    pub duration: Duration,
    /// Captured tail of the child's stderr (bounded by `STDERR_CAPTURE_BYTES`).
    /// Empty when the spawn itself failed before stderr could be read.
    pub stderr: String,
}

enum CommandState {
    Idle,
    Running {
        label: String,
        started_at: Instant,
        rx: mpsc::Receiver<CommandResult>,
    },
}

pub struct CommandRunner {
    state: CommandState,
    /// Ephemeral status-bar message set on command completion; cleared after MESSAGE_TTL.
    pub(crate) message: Option<(String, Instant)>,
    /// Absolute path to `coordinator.yml` found at startup.
    ///
    /// Searched by walking up from the working directory at launch time, so
    /// the TUI works correctly regardless of which directory it was invoked
    /// from. When `Some`, every spawned `coord` subcommand receives
    /// `--config <path>` so it locates the right config file. When `None`,
    /// the status bar shows a warning and commands will fail.
    pub(crate) config_path: Option<PathBuf>,
}

/// Search for `coordinator.yml` in three places, in order:
///
/// 1. The path in the `COORD_CONFIG` env var (when set and existing).
///    Lets the user launch from anywhere — e.g. when the binary is
///    installed to `~/.local/bin` and they want to drive their primary
///    config from `$HOME` or `/tmp`.
/// 2. The first ancestor of `cwd` that contains `coordinator.yml`.
///    Mirrors how `git` finds `.git/` and stays robust when launched
///    from a subdirectory of the project.
/// 3. `~/.coord/coordinator.yml` — a symlink-friendly default for
///    users with a single primary config.
///
/// Returns `None` only when none of the three resolve.
fn find_config() -> Option<PathBuf> {
    find_config_with(
        std::env::var_os("COORD_CONFIG").map(PathBuf::from),
        std::env::current_dir().ok(),
        std::env::var_os("HOME").map(PathBuf::from),
    )
}

/// Pure resolver used by [`find_config`].  Split out so tests can exercise
/// the precedence order without mutating process env state.
fn find_config_with(
    env_override: Option<PathBuf>,
    cwd: Option<PathBuf>,
    home_dir: Option<PathBuf>,
) -> Option<PathBuf> {
    if let Some(path) = env_override {
        if path.exists() {
            return Some(path);
        }
    }
    if let Some(mut dir) = cwd {
        loop {
            let candidate = dir.join("coordinator.yml");
            if candidate.exists() {
                return Some(candidate);
            }
            // `pop()` returns false when we've reached the root.
            if !dir.pop() {
                break;
            }
        }
    }
    if let Some(home) = home_dir {
        let candidate = home.join(".coord").join("coordinator.yml");
        if candidate.exists() {
            return Some(candidate);
        }
    }
    None
}

impl CommandRunner {
    pub fn new() -> Self {
        Self {
            state: CommandState::Idle,
            message: None,
            config_path: find_config(),
        }
    }

    /// Spawn `coord <args>` in a background thread.
    ///
    /// For real subcommands (i.e. `args[0]` does not start with `-`), this
    /// injects `--config <absolute_path>` immediately after the subcommand
    /// name so that `coord` can locate `coordinator.yml` even when the TUI
    /// was launched from a different working directory than the project root.
    ///
    /// Returns `false` if a command is already running.
    pub fn spawn(&mut self, args: &[&str]) -> bool {
        if self.is_running() {
            return false;
        }
        let label = format!("coord {}", args.join(" "));
        let (tx, rx) = mpsc::channel();

        // Build the full argument list, injecting --config after the subcommand
        // name (but not for flag-style args like --version which start with '-').
        let full_args: Vec<String> = {
            let mut v: Vec<String> = Vec::with_capacity(args.len() + 2);
            let mut iter = args.iter();
            if let Some(first) = iter.next() {
                v.push(first.to_string());
                if !first.starts_with('-') {
                    if let Some(cfg) = &self.config_path {
                        v.push("--config".to_string());
                        v.push(cfg.to_string_lossy().into_owned());
                    }
                }
                for a in iter {
                    v.push(a.to_string());
                }
            }
            v
        };

        let label_clone = label.clone();
        std::thread::spawn(move || {
            let started = Instant::now();
            // Belt-and-braces: main.rs sets GIT_TERMINAL_PROMPT=0 and
            // ssh BatchMode=yes so descendants can't prompt, but explicitly
            // null-out stdin here so even a directly-invoked credential
            // helper can't grab the TUI's TTY.
            //
            // stdout stays piped to /dev/null (#251 — keeps a chatty child
            // from blocking on a full pipe buffer).  stderr is captured up
            // to STDERR_CAPTURE_BYTES so failure reasons (gh errors, missing
            // labels, auth prompts, etc.) can be toasted instead of vanishing
            // into the void.
            let spawn_result = Command::new("coord")
                .args(&full_args)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::piped())
                .spawn();
            let result = match spawn_result {
                Ok(mut child) => {
                    // Drain stderr concurrently with wait — otherwise a child
                    // that writes more than the pipe buffer (~64 KiB) blocks
                    // forever waiting for us to read.
                    let stderr_handle = child.stderr.take();
                    let reader = std::thread::spawn(move || {
                        let mut buf = String::new();
                        if let Some(mut s) = stderr_handle {
                            // Drain fully so the child isn't blocked on a
                            // full pipe; truncate after.  Coord shellouts
                            // emit kilobytes at most, so the transient memory
                            // cost is bounded in practice.
                            let mut sink: Vec<u8> = Vec::new();
                            let _ = s.read_to_end(&mut sink);
                            buf = String::from_utf8_lossy(&sink).into_owned();
                            if buf.len() > STDERR_CAPTURE_BYTES {
                                buf.truncate(STDERR_CAPTURE_BYTES);
                                buf.push_str("…[truncated]");
                            }
                        }
                        buf
                    });
                    let exit_code = child.wait()
                        .map(|s| s.code().unwrap_or(-1))
                        .unwrap_or(-1);
                    let stderr = reader.join().unwrap_or_default();
                    CommandResult {
                        label: label_clone,
                        exit_code,
                        duration: started.elapsed(),
                        stderr,
                    }
                }
                Err(_) => CommandResult {
                    label: label_clone,
                    exit_code: -1,
                    duration: started.elapsed(),
                    stderr: String::new(),
                },
            };
            let _ = tx.send(result);
        });
        self.state = CommandState::Running {
            label,
            started_at: Instant::now(),
            rx,
        };
        true
    }

    /// Non-blocking check for command completion.
    ///
    /// Returns `Some(result)` when a command just finished (caller can inspect
    /// `exit_code` and `stderr` to surface failure toasts); returns `None`
    /// while the command is still running or no command is queued.
    pub fn poll(&mut self) -> Option<CommandResult> {
        let rx = match &self.state {
            CommandState::Running { rx, .. } => rx,
            CommandState::Idle => return None,
        };
        match rx.try_recv() {
            Ok(result) => {
                let msg = if result.exit_code == 0 {
                    format!("{} done ({:.1}s)", result.label, result.duration.as_secs_f64())
                } else {
                    format!("{} failed (exit {})", result.label, result.exit_code)
                };
                self.message = Some((msg, Instant::now()));
                self.state = CommandState::Idle;
                Some(result)
            }
            Err(mpsc::TryRecvError::Empty) => None,
            Err(mpsc::TryRecvError::Disconnected) => {
                self.message = Some(("command thread lost".into(), Instant::now()));
                self.state = CommandState::Idle;
                None
            }
        }
    }

    pub fn is_running(&self) -> bool {
        matches!(self.state, CommandState::Running { .. })
    }

    /// Returns (label, elapsed) if a command is currently running.
    pub fn running_info(&self) -> Option<(&str, Duration)> {
        match &self.state {
            CommandState::Running {
                label, started_at, ..
            } => Some((label.as_str(), started_at.elapsed())),
            CommandState::Idle => None,
        }
    }

}

#[cfg(test)]
mod tests {
    use super::*;

    /// Poll the runner up to ~10 s waiting for completion.  Replaces the
    /// fixed `sleep(2s) + poll()` pattern that flaked under parallel-suite
    /// CPU contention (coord --version boots Python and can take >2 s when
    /// 360 tests share cores).
    fn wait_for_result(runner: &mut CommandRunner) -> Option<CommandResult> {
        for _ in 0..100 {
            if let Some(r) = runner.poll() {
                return Some(r);
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        None
    }

    #[test]
    fn spawn_returns_false_when_busy() {
        let mut runner = CommandRunner::new();
        assert!(runner.spawn(&["--version"]));
        assert!(!runner.spawn(&["--version"]));
        assert!(wait_for_result(&mut runner).is_some(), "command did not finish within 10s");
    }

    #[test]
    fn poll_captures_result() {
        let mut runner = CommandRunner::new();
        runner.spawn(&["--version"]);
        let result = wait_for_result(&mut runner)
            .expect("command did not finish within 10s");
        assert!(!runner.is_running());
        assert_eq!(result.exit_code, 0, "coord --version should succeed");
        let (msg, _) = runner.message.as_ref().expect("message set on completion");
        assert!(msg.contains("done"), "expected success message, got: {msg}");
    }

    #[test]
    fn config_path_is_absolute_when_found() {
        // When find_config() returns Some, the path must be absolute so it
        // remains valid regardless of subsequent directory changes.
        if let Some(path) = find_config() {
            assert!(path.is_absolute(), "config_path should be absolute, got: {:?}", path);
            assert!(path.exists(), "config_path should exist: {:?}", path);
        }
        // None is also valid (no coordinator.yml in the ancestor chain).
    }

    #[test]
    fn spawn_injects_config_for_subcommand() {
        // Build a runner that has a known config path and verify the
        // injected args include --config before the extra args.
        let mut runner = CommandRunner::new();
        // Override with a synthetic absolute path (file need not exist for
        // this structural test).
        runner.config_path = Some(PathBuf::from("/tmp/test/coordinator.yml"));

        // We can't easily inspect the args after they're passed to the thread,
        // but we can verify that spawn() returns true (i.e. didn't panic while
        // building full_args) and that the runner transitions to Running state.
        assert!(runner.spawn(&["notify"]));
        assert!(runner.is_running());
    }

    #[test]
    fn spawn_no_inject_for_flag_args() {
        // When args[0] starts with '-' (e.g. --version), no --config injection
        // should happen. We verify by spawning --version and checking it succeeds.
        let mut runner = CommandRunner::new();
        runner.config_path = Some(PathBuf::from("/nonexistent/coordinator.yml"));
        assert!(runner.spawn(&["--version"]));
        assert!(wait_for_result(&mut runner).is_some(), "command did not finish within 10s");
        // coord --version should succeed regardless of the config path —
        // the completion message records "done" rather than "failed".
        let (msg, _) = runner.message.as_ref().expect("message set on completion");
        assert!(msg.contains("done"), "expected success message, got: {msg}");
    }

    /// Helper: build an isolated temp directory tree for resolver tests.
    /// Returns `(root, cwd, home)` where `cwd` is a nested subdir under
    /// `root` and `home` is a sibling.  None of the three contain a
    /// `coordinator.yml` — tests create those as needed.
    fn make_resolver_tmp() -> (PathBuf, PathBuf, PathBuf) {
        let unique = format!(
            "coord-find-config-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        );
        let root = std::env::temp_dir().join(unique);
        let cwd = root.join("project").join("sub");
        let home = root.join("home");
        std::fs::create_dir_all(&cwd).unwrap();
        std::fs::create_dir_all(home.join(".coord")).unwrap();
        (root, cwd, home)
    }

    #[test]
    fn find_config_env_override_wins() {
        // COORD_CONFIG should beat both the ancestor walk and the home fallback.
        let (root, cwd, home) = make_resolver_tmp();
        let env_path = root.join("override.yml");
        std::fs::write(&env_path, "repos: []").unwrap();
        // Also create competing candidates in the lower-priority slots.
        std::fs::write(cwd.join("coordinator.yml"), "repos: []").unwrap();
        std::fs::write(home.join(".coord").join("coordinator.yml"), "repos: []").unwrap();

        let found = find_config_with(Some(env_path.clone()), Some(cwd.clone()), Some(home.clone()));
        assert_eq!(found.as_deref(), Some(env_path.as_path()));
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn find_config_env_override_ignored_when_missing() {
        // A nonexistent COORD_CONFIG must NOT shadow the ancestor walk —
        // otherwise a stale env var would brick the resolver.
        let (root, cwd, home) = make_resolver_tmp();
        let ancestor_yml = cwd.parent().unwrap().join("coordinator.yml");
        std::fs::write(&ancestor_yml, "repos: []").unwrap();

        let found = find_config_with(
            Some(PathBuf::from("/nonexistent/override.yml")),
            Some(cwd),
            Some(home),
        );
        assert_eq!(found.as_deref(), Some(ancestor_yml.as_path()));
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn find_config_walks_up_to_ancestor() {
        let (root, cwd, home) = make_resolver_tmp();
        let ancestor_yml = cwd.parent().unwrap().join("coordinator.yml");
        std::fs::write(&ancestor_yml, "repos: []").unwrap();

        let found = find_config_with(None, Some(cwd), Some(home));
        assert_eq!(found.as_deref(), Some(ancestor_yml.as_path()));
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn find_config_home_fallback_when_no_ancestor_match() {
        // No env override, no ancestor coordinator.yml → home fallback wins.
        let (root, cwd, home) = make_resolver_tmp();
        let home_yml = home.join(".coord").join("coordinator.yml");
        std::fs::write(&home_yml, "repos: []").unwrap();

        let found = find_config_with(None, Some(cwd), Some(home));
        assert_eq!(found.as_deref(), Some(home_yml.as_path()));
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn find_config_returns_none_when_nothing_resolves() {
        let (root, cwd, home) = make_resolver_tmp();
        let found = find_config_with(None, Some(cwd), Some(home));
        assert_eq!(found, None);
        std::fs::remove_dir_all(&root).ok();
    }
}
