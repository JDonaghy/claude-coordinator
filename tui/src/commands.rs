use std::collections::VecDeque;
use std::io::Read;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

/// Maximum stderr bytes captured per command.  Bounds memory + toast size when
/// a worker is extremely verbose; the head is the most useful bit for diagnosing
/// shell-level failures (`gh: not found`, missing label, auth prompt, etc.).
const STDERR_CAPTURE_BYTES: usize = 2048;
/// Bounds captured stdout (e.g. `coord diagnose`'s findings summary, which the
/// TUI toasts back to the operator).  Larger than stderr — diagnose output is
/// the *useful* payload here, not just a failure reason.
const STDOUT_CAPTURE_BYTES: usize = 8192;

pub struct CommandResult {
    pub label: String,
    pub exit_code: i32,
    pub duration: Duration,
    /// Captured tail of the child's stderr (bounded by `STDERR_CAPTURE_BYTES`).
    /// Empty when the spawn itself failed before stderr could be read.
    pub stderr: String,
    /// Captured stdout (bounded by `STDOUT_CAPTURE_BYTES`).  Most commands write
    /// nothing useful here, but `coord diagnose` writes its findings/actions +
    /// the `DIAGNOSE_RESULT:` trailer the TUI parses to decide whether to offer
    /// a reset.  Empty when the spawn failed before stdout could be read.
    pub stdout: String,
}

/// Outcome returned by [`CommandRunner::spawn_queued`].
#[derive(Debug, PartialEq, Eq)]
pub enum SpawnQueuedOutcome {
    /// The runner was idle; the command started immediately.
    Started,
    /// A different command was already running; this command was added to the
    /// FIFO queue and will run automatically after the current command completes.
    Queued,
    /// An identical command (same argv) is already running or already pending
    /// in the queue; the duplicate was silently dropped.
    Deduped,
}

enum CommandState {
    Idle,
    Running {
        label: String,
        /// Raw args (before `--config` injection), stored for dedup checks in
        /// [`CommandRunner::spawn_queued`].
        argv: Vec<String>,
        started_at: Instant,
        rx: mpsc::Receiver<CommandResult>,
    },
}

pub struct CommandRunner {
    state: CommandState,
    /// FIFO queue for user-initiated commands enqueued while another is running.
    /// Each entry is the raw argv (before `--config` injection).  The front of
    /// the queue is popped and started automatically each time [`poll`] detects
    /// command completion.
    queue: VecDeque<Vec<String>>,
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
pub(crate) fn find_config() -> Option<PathBuf> {
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
            queue: VecDeque::new(),
            message: None,
            config_path: find_config(),
        }
    }

    /// Internal: unconditionally start `coord <argv>` in a background thread
    /// and transition to `Running` state.  Caller **must** verify the runner
    /// is [`CommandState::Idle`] before calling.
    ///
    /// For real subcommands (i.e. `argv[0]` does not start with `-`) this
    /// injects `--config <absolute_path>` into the child's argument list so
    /// `coord` locates `coordinator.yml` even when the TUI was launched from
    /// a different working directory.  The injected flag is NOT reflected in
    /// `label` or the stored `argv` (kept clean for status display and dedup).
    fn do_spawn(&mut self, argv: Vec<String>) {
        let label = format!("coord {}", argv.join(" "));
        let (tx, rx) = mpsc::channel();

        // Build the full argument list, injecting --config after the subcommand
        // name (but not for flag-style args like --version which start with '-').
        let full_args: Vec<String> = {
            let mut v: Vec<String> = Vec::with_capacity(argv.len() + 2);
            let mut iter = argv.iter();
            if let Some(first) = iter.next() {
                v.push(first.clone());
                if !first.starts_with('-') {
                    if let Some(cfg) = &self.config_path {
                        v.push("--config".to_string());
                        v.push(cfg.to_string_lossy().into_owned());
                    }
                }
                for a in iter {
                    v.push(a.clone());
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
            // stdout is now piped + drained concurrently (was /dev/null, #251):
            // `coord diagnose` reports its findings on stdout, and the
            // concurrent drain below keeps a chatty child from blocking on a
            // full pipe just as it does for stderr.
            let spawn_result = Command::new("coord")
                .args(&full_args)
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
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
                    // Drain stdout the same way (concurrent with wait).
                    let stdout_handle = child.stdout.take();
                    let out_reader = std::thread::spawn(move || {
                        let mut buf = String::new();
                        if let Some(mut s) = stdout_handle {
                            let mut sink: Vec<u8> = Vec::new();
                            let _ = s.read_to_end(&mut sink);
                            buf = String::from_utf8_lossy(&sink).into_owned();
                            if buf.len() > STDOUT_CAPTURE_BYTES {
                                // Keep the TAIL: the DIAGNOSE_RESULT trailer and
                                // the most recent actions are at the end.  Walk
                                // forward to a char boundary so slicing a
                                // multi-byte codepoint can't panic.
                                let mut start = buf.len() - STDOUT_CAPTURE_BYTES;
                                while start < buf.len() && !buf.is_char_boundary(start) {
                                    start += 1;
                                }
                                buf = format!("…[truncated]\n{}", &buf[start..]);
                            }
                        }
                        buf
                    });
                    let exit_code = child.wait()
                        .map(|s| s.code().unwrap_or(-1))
                        .unwrap_or(-1);
                    let stderr = reader.join().unwrap_or_default();
                    let stdout = out_reader.join().unwrap_or_default();
                    CommandResult {
                        label: label_clone,
                        exit_code,
                        duration: started.elapsed(),
                        stderr,
                        stdout,
                    }
                }
                Err(_) => CommandResult {
                    label: label_clone,
                    exit_code: -1,
                    duration: started.elapsed(),
                    stderr: String::new(),
                    stdout: String::new(),
                },
            };
            let _ = tx.send(result);
        });
        self.state = CommandState::Running {
            label,
            argv,
            started_at: Instant::now(),
            rx,
        };
    }

    /// Spawn `coord <args>` in a background thread.
    ///
    /// For real subcommands (i.e. `args[0]` does not start with `-`), this
    /// injects `--config <absolute_path>` immediately after the subcommand
    /// name so that `coord` can locate `coordinator.yml` even when the TUI
    /// was launched from a different working directory than the project root.
    ///
    /// Returns `false` if a command is already running.  For user-initiated
    /// actions that should queue instead of refuse, use [`spawn_queued`].
    ///
    /// # Audit rule
    ///
    /// **User-initiated actions** (keybinds, toolbar buttons, context-menu items)
    /// must call [`spawn_queued`] so the action queues with visible feedback
    /// instead of being silently refused with "another command is running".
    ///
    /// **Background / auto-fire callers** (e.g. `kick_issue_sync`, auto-notify)
    /// must call this method (`spawn`) so they **skip when busy** rather than
    /// piling up in the queue.  These paths are always guarded by an explicit
    /// `is_running()` check before calling.
    pub fn spawn(&mut self, args: &[&str]) -> bool {
        if self.is_running() {
            return false;
        }
        let argv: Vec<String> = args.iter().map(|s| s.to_string()).collect();
        self.do_spawn(argv);
        true
    }

    /// Spawn `coord <args>`, queuing the command if another is already running.
    ///
    /// Behaviour:
    /// - **Idle**: starts the command immediately, returns [`SpawnQueuedOutcome::Started`].
    /// - **Busy, different command**: adds to the FIFO queue; the command runs
    ///   automatically after the current one completes.  Returns
    ///   [`SpawnQueuedOutcome::Queued`].
    /// - **Busy, identical argv**: the same command is already running *or*
    ///   already waiting in the queue; the duplicate is silently dropped.
    ///   Returns [`SpawnQueuedOutcome::Deduped`].
    ///
    /// Use this for **user-initiated** actions.  Periodic background callers
    /// (e.g. `coord sync --quiet` tick) must continue to call [`spawn`] so
    /// they skip when busy rather than pile up in the queue.
    pub fn spawn_queued(&mut self, args: &[&str]) -> SpawnQueuedOutcome {
        let argv: Vec<String> = args.iter().map(|s| s.to_string()).collect();

        // Dedup: identical argv already running?
        if let CommandState::Running { argv: running_argv, .. } = &self.state {
            if *running_argv == argv {
                return SpawnQueuedOutcome::Deduped;
            }
        }

        // Dedup: identical argv already queued?
        if self.queue.iter().any(|q| q == &argv) {
            return SpawnQueuedOutcome::Deduped;
        }

        if !self.is_running() {
            self.do_spawn(argv);
            SpawnQueuedOutcome::Started
        } else {
            self.queue.push_back(argv);
            SpawnQueuedOutcome::Queued
        }
    }

    /// Number of commands waiting in the queue (not counting the one
    /// currently running).  Used by the status bar to show `· N queued`.
    pub fn queue_depth(&self) -> usize {
        self.queue.len()
    }

    /// Non-blocking check for command completion.
    ///
    /// Returns `Some(result)` when a command just finished (caller can inspect
    /// `exit_code` and `stderr` to surface failure toasts); returns `None`
    /// while the command is still running or no command is queued.
    ///
    /// When a command completes, any command waiting at the front of the queue
    /// is popped and started automatically before this method returns.
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
                // Pop and start the next queued command, if any.
                if let Some(next_argv) = self.queue.pop_front() {
                    self.do_spawn(next_argv);
                }
                Some(result)
            }
            Err(mpsc::TryRecvError::Empty) => None,
            Err(mpsc::TryRecvError::Disconnected) => {
                self.message = Some(("command thread lost".into(), Instant::now()));
                self.state = CommandState::Idle;
                // Pop and start the next queued command even on disconnect.
                if let Some(next_argv) = self.queue.pop_front() {
                    self.do_spawn(next_argv);
                }
                None
            }
        }
    }

    pub fn is_running(&self) -> bool {
        matches!(self.state, CommandState::Running { .. })
    }

    /// Returns `(label, elapsed)` if a command is currently running.
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

    // ── Queue tests ───────────────────────────────────────────────────────────

    /// A command enqueued while another is running starts automatically once
    /// the first command completes (via `poll()`).
    #[test]
    fn queue_runs_after_first_completes() {
        let mut runner = CommandRunner::new();
        // Start first command.
        assert!(runner.spawn(&["--version"]));
        assert!(runner.is_running());
        // Enqueue a second command while the first is still running.
        let outcome = runner.spawn_queued(&["--help"]);
        assert_eq!(outcome, SpawnQueuedOutcome::Queued);
        assert_eq!(runner.queue_depth(), 1);
        // Wait for the first command to complete; poll() should auto-start the second.
        let result_a = wait_for_result(&mut runner)
            .expect("first command did not finish within 10s");
        assert!(result_a.label.contains("--version"), "unexpected label: {}", result_a.label);
        // Second command should now be running automatically.
        assert!(runner.is_running(), "second command should have started after first completed");
        assert_eq!(runner.queue_depth(), 0);
        // Wait for the second command to finish.
        let result_b = wait_for_result(&mut runner)
            .expect("second (queued) command did not finish within 10s");
        assert!(result_b.label.contains("--help"), "unexpected label: {}", result_b.label);
        assert!(!runner.is_running());
    }

    /// Commands in the queue run in FIFO order.
    #[test]
    fn queue_maintains_fifo_order() {
        let mut runner = CommandRunner::new();
        // Start first command.
        assert!(runner.spawn(&["--version"]));
        // Enqueue two more.
        assert_eq!(runner.spawn_queued(&["--help"]), SpawnQueuedOutcome::Queued);
        assert_eq!(runner.spawn_queued(&["version"]), SpawnQueuedOutcome::Queued);
        assert_eq!(runner.queue_depth(), 2);
        // First completes → second (notify) starts.
        wait_for_result(&mut runner).expect("first");
        assert!(runner.is_running(), "second command should have started");
        let (label_b, _) = runner.running_info().expect("running_info");
        assert!(
            label_b.contains("--help"),
            "expected 'notify' to run second (FIFO), got: {label_b}",
        );
        assert_eq!(runner.queue_depth(), 1);
        // Second completes → third (sync --quiet) starts.
        wait_for_result(&mut runner).expect("second");
        assert!(runner.is_running(), "third command should have started");
        let (label_c, _) = runner.running_info().expect("running_info");
        assert!(
            label_c.contains("version"),
            "expected 'sync' to run third (FIFO), got: {label_c}",
        );
        assert_eq!(runner.queue_depth(), 0);
        // Third completes.
        wait_for_result(&mut runner).expect("third");
        assert!(!runner.is_running());
    }

    /// `spawn_queued` drops a command whose argv is identical to the one
    /// currently running, and also drops a command that is already pending.
    #[test]
    fn spawn_queued_deduplicates_identical_commands() {
        let mut runner = CommandRunner::new();
        // Start a command.
        assert!(runner.spawn(&["--version"]));
        // Same argv as running command → deduped.
        assert_eq!(
            runner.spawn_queued(&["--version"]),
            SpawnQueuedOutcome::Deduped,
            "should dedup against the running command",
        );
        assert_eq!(runner.queue_depth(), 0);
        // Different argv → queued.
        assert_eq!(runner.spawn_queued(&["--help"]), SpawnQueuedOutcome::Queued);
        assert_eq!(runner.queue_depth(), 1);
        // Same argv as the queued command → deduped.
        assert_eq!(
            runner.spawn_queued(&["--help"]),
            SpawnQueuedOutcome::Deduped,
            "should dedup against already-queued command",
        );
        assert_eq!(runner.queue_depth(), 1, "dedup must not inflate the queue");
        // Let everything finish.
        wait_for_result(&mut runner).unwrap(); // --version done, notify starts
        wait_for_result(&mut runner).unwrap(); // notify done
        assert!(!runner.is_running());
        assert_eq!(runner.queue_depth(), 0);
    }

    /// `queue_depth()` accurately reflects the number of pending commands
    /// as items drain through the queue.
    #[test]
    fn queue_depth_reflects_pending_count() {
        let mut runner = CommandRunner::new();
        assert_eq!(runner.queue_depth(), 0, "starts empty");
        // Start first command; queue should still be empty (it's running, not queued).
        assert!(runner.spawn(&["--version"]));
        assert_eq!(runner.queue_depth(), 0);
        // Enqueue two more.
        runner.spawn_queued(&["--help"]);
        assert_eq!(runner.queue_depth(), 1);
        runner.spawn_queued(&["version"]);
        assert_eq!(runner.queue_depth(), 2);
        // First completes → second starts; only the third remains queued.
        wait_for_result(&mut runner).expect("first");
        assert_eq!(runner.queue_depth(), 1, "sync should still be queued");
        // Second completes → third starts; queue is now empty.
        wait_for_result(&mut runner).expect("second");
        assert_eq!(runner.queue_depth(), 0, "queue should be drained after second pop");
        // Third completes; all done.
        wait_for_result(&mut runner).expect("third");
        assert_eq!(runner.queue_depth(), 0);
        assert!(!runner.is_running());
    }

    // ── Migration rule tests (#428) ───────────────────────────────────────────

    /// User-initiated action: `spawn_queued` when runner is busy returns `Queued`
    /// (not `false`/`Deduped`).  Simulates the migrate-to-queue path for actions
    /// like stop, retry, notify, merge, agent restart.
    #[test]
    fn spawn_queued_returns_queued_when_busy() {
        let mut runner = CommandRunner::new();
        // Start a "current" command.
        assert_eq!(runner.spawn_queued(&["--version"]), SpawnQueuedOutcome::Started);
        assert!(runner.is_running());
        // A different user-initiated action arrives while the first runs.
        let outcome = runner.spawn_queued(&["--help"]);
        assert_eq!(
            outcome,
            SpawnQueuedOutcome::Queued,
            "user-initiated action should queue, not refuse",
        );
        assert_eq!(runner.queue_depth(), 1);
        // Let everything drain.
        wait_for_result(&mut runner).unwrap();
        wait_for_result(&mut runner).unwrap();
        assert!(!runner.is_running());
    }

    /// Background `spawn()` callers skip when busy — the skip-when-busy
    /// invariant must not be broken by the migration.
    #[test]
    fn background_spawn_skips_when_busy() {
        let mut runner = CommandRunner::new();
        assert!(runner.spawn(&["--version"]), "first spawn should succeed");
        assert!(runner.is_running());
        // Background caller (e.g. kick_issue_sync) calls plain spawn() — must skip.
        assert!(
            !runner.spawn(&["--help"]),
            "background spawn should return false (skip), not queue",
        );
        assert_eq!(runner.queue_depth(), 0, "background spawn must not add to queue");
        wait_for_result(&mut runner).unwrap();
        assert!(!runner.is_running());
    }

    /// Identical commands across both `spawn` (running) and `spawn_queued`
    /// (pending) are silently deduped — no scary error, queue stays at 1.
    #[test]
    fn spawn_queued_dedup_prevents_pile_up() {
        let mut runner = CommandRunner::new();
        assert!(runner.spawn(&["--version"]));
        // Queue one instance of --help.
        assert_eq!(runner.spawn_queued(&["--help"]), SpawnQueuedOutcome::Queued);
        assert_eq!(runner.queue_depth(), 1);
        // Mash the same action three more times — all should dedup.
        for _ in 0..3 {
            assert_eq!(
                runner.spawn_queued(&["--help"]),
                SpawnQueuedOutcome::Deduped,
                "repeated identical action should dedup silently",
            );
        }
        assert_eq!(runner.queue_depth(), 1, "dedup must not inflate queue past 1");
        // Drain.
        wait_for_result(&mut runner).unwrap();
        wait_for_result(&mut runner).unwrap();
        assert!(!runner.is_running());
    }

    /// `spawn_queued` when the runner is idle returns `Started` immediately —
    /// the `Queued` path is never taken when there is nothing to wait for.
    #[test]
    fn spawn_queued_starts_immediately_when_idle() {
        let mut runner = CommandRunner::new();
        assert!(!runner.is_running(), "runner should start idle");
        let outcome = runner.spawn_queued(&["--version"]);
        assert_eq!(
            outcome,
            SpawnQueuedOutcome::Started,
            "should start immediately when idle",
        );
        assert!(runner.is_running());
        assert_eq!(runner.queue_depth(), 0);
        wait_for_result(&mut runner).unwrap();
    }
}
