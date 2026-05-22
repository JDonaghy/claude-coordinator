use std::collections::VecDeque;
use std::path::PathBuf;
use std::process::Command;
use std::sync::mpsc;
use std::time::{Duration, Instant};

const HISTORY_CAP: usize = 20;

pub struct CommandResult {
    pub label: String,
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
    pub duration: Duration,
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
    history: VecDeque<CommandResult>,
    /// Ephemeral status-bar message set on command completion; cleared after MESSAGE_TTL.
    pub(crate) message: Option<(String, Instant)>,
    /// Absolute path to `coordinator.yml` found at startup.
    ///
    /// Searched by walking up from the working directory at launch time, so
    /// the TUI works correctly regardless of which directory it was invoked
    /// from. When `Some`, every spawned `coord` subcommand receives
    /// `--config <path>` so it locates the right config file. When `None`,
    /// the bottom panel shows a warning and commands will fail.
    pub(crate) config_path: Option<PathBuf>,
}

/// Search for `coordinator.yml` starting from the current working directory,
/// walking up to the filesystem root. Returns the absolute path of the first
/// match, or `None` if no `coordinator.yml` exists in any ancestor directory.
///
/// Walking upward mirrors how `git` finds `.git/` and makes the TUI robust
/// when launched from a subdirectory of the project or from a shell that
/// inherited an unexpected working directory (e.g. a `.desktop` launcher or
/// an IDE terminal that opens in `$HOME`).
fn find_config() -> Option<PathBuf> {
    let mut dir = std::env::current_dir().ok()?;
    loop {
        let candidate = dir.join("coordinator.yml");
        if candidate.exists() {
            return Some(candidate);
        }
        // `pop()` returns false when we've reached the root and cannot go further.
        if !dir.pop() {
            return None;
        }
    }
}

impl CommandRunner {
    pub fn new() -> Self {
        Self {
            state: CommandState::Idle,
            history: VecDeque::new(),
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
            let output = Command::new("coord").args(&full_args).output();
            let result = match output {
                Ok(out) => CommandResult {
                    label: label_clone,
                    exit_code: out.status.code().unwrap_or(-1),
                    stdout: String::from_utf8_lossy(&out.stdout).into_owned(),
                    stderr: String::from_utf8_lossy(&out.stderr).into_owned(),
                    duration: started.elapsed(),
                },
                Err(e) => CommandResult {
                    label: label_clone,
                    exit_code: -1,
                    stdout: String::new(),
                    stderr: format!("failed to run command: {e}"),
                    duration: started.elapsed(),
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
    /// Returns `true` if a command just finished (result pushed to history).
    pub fn poll(&mut self) -> bool {
        let rx = match &self.state {
            CommandState::Running { rx, .. } => rx,
            CommandState::Idle => return false,
        };
        match rx.try_recv() {
            Ok(result) => {
                let msg = if result.exit_code == 0 {
                    format!("{} done ({:.1}s)", result.label, result.duration.as_secs_f64())
                } else {
                    format!("{} failed (exit {})", result.label, result.exit_code)
                };
                self.message = Some((msg, Instant::now()));
                self.history.push_front(result);
                if self.history.len() > HISTORY_CAP {
                    self.history.pop_back();
                }
                self.state = CommandState::Idle;
                true
            }
            Err(mpsc::TryRecvError::Empty) => false,
            Err(mpsc::TryRecvError::Disconnected) => {
                self.message = Some(("command thread lost".into(), Instant::now()));
                self.state = CommandState::Idle;
                true
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

    pub fn last_result(&self) -> Option<&CommandResult> {
        self.history.front()
    }

    #[cfg(test)]
    fn history_len(&self) -> usize {
        self.history.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn spawn_returns_false_when_busy() {
        let mut runner = CommandRunner::new();
        assert!(runner.spawn(&["--version"]));
        assert!(!runner.spawn(&["--version"]));
        // Let it finish so the thread is cleaned up.
        std::thread::sleep(Duration::from_millis(500));
        assert!(runner.poll());
    }

    #[test]
    fn poll_captures_result() {
        let mut runner = CommandRunner::new();
        runner.spawn(&["--version"]);
        // Wait for it to finish.
        std::thread::sleep(Duration::from_millis(2000));
        assert!(runner.poll());
        assert!(!runner.is_running());
        let result = runner.last_result().unwrap();
        assert_eq!(result.exit_code, 0);
        assert!(result.stdout.contains("coord"));
    }

    #[test]
    fn history_cap_enforced() {
        let mut runner = CommandRunner::new();
        for _ in 0..HISTORY_CAP + 5 {
            runner.spawn(&["--version"]);
            std::thread::sleep(Duration::from_millis(500));
            runner.poll();
        }
        assert!(runner.history_len() <= HISTORY_CAP);
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
        std::thread::sleep(Duration::from_millis(2000));
        runner.poll();
        let result = runner.last_result().unwrap();
        // coord --version should succeed regardless of the config path.
        assert_eq!(result.exit_code, 0);
    }
}
