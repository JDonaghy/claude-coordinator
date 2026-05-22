use std::collections::VecDeque;
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
}

impl CommandRunner {
    pub fn new() -> Self {
        Self {
            state: CommandState::Idle,
            history: VecDeque::new(),
            message: None,
        }
    }

    /// Spawn `coord <args>` in a background thread.
    /// Returns `false` if a command is already running.
    pub fn spawn(&mut self, args: &[&str]) -> bool {
        if self.is_running() {
            return false;
        }
        let label = format!("coord {}", args.join(" "));
        let (tx, rx) = mpsc::channel();
        let args_owned: Vec<String> = args.iter().map(|s| s.to_string()).collect();
        let label_clone = label.clone();
        std::thread::spawn(move || {
            let started = Instant::now();
            let output = Command::new("coord").args(&args_owned).output();
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
}
