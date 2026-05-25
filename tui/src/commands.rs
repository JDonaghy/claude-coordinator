use std::collections::VecDeque;
use std::path::PathBuf;
use std::process::{Command, Stdio};
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
            // Belt-and-braces: main.rs sets GIT_TERMINAL_PROMPT=0 and
            // ssh BatchMode=yes so descendants can't prompt, but explicitly
            // null-out stdin here so even a directly-invoked credential
            // helper can't grab the TUI's TTY.
            let output = Command::new("coord")
                .args(&full_args)
                .stdin(Stdio::null())
                .output();
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
