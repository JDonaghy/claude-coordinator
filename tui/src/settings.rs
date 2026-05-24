//! User-facing TUI settings persisted to `~/.coord/settings.toml`.
//!
//! [`TuiSettings`] is loaded once at startup and saved whenever a field
//! changes through the settings panel.  The file is human-editable TOML;
//! unknown keys are silently ignored on load so forward-compatible upgrades
//! don't break older binaries.

use std::collections::HashMap;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

// ─── Refresh cadence ─────────────────────────────────────────────────────────

/// How often the TUI re-reads the SQLite database.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum RefreshCadence {
    OneSec,
    #[default]
    FiveSec,
    ThirtySec,
    Off,
}

impl RefreshCadence {
    /// Labels shown in the SegmentedControl, in order.
    pub const LABELS: &'static [&'static str] = &["1s", "5s", "30s", "Off"];

    pub fn from_idx(idx: usize) -> Self {
        match idx {
            0 => Self::OneSec,
            1 => Self::FiveSec,
            2 => Self::ThirtySec,
            _ => Self::Off,
        }
    }

    pub fn to_idx(self) -> usize {
        match self {
            Self::OneSec => 0,
            Self::FiveSec => 1,
            Self::ThirtySec => 2,
            Self::Off => 3,
        }
    }

    /// Returns `None` when cadence is `Off` (no auto-refresh).
    pub fn as_duration(self) -> Option<std::time::Duration> {
        match self {
            Self::OneSec => Some(std::time::Duration::from_secs(1)),
            Self::FiveSec => Some(std::time::Duration::from_secs(5)),
            Self::ThirtySec => Some(std::time::Duration::from_secs(30)),
            Self::Off => None,
        }
    }
}

// ─── Log cache TTL ───────────────────────────────────────────────────────────

/// How long the watch-overlay log cache is considered fresh before re-fetching.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum LogCacheTtl {
    OneSec,
    #[default]
    TwoSec,
    FiveSec,
}

impl LogCacheTtl {
    pub const LABELS: &'static [&'static str] = &["1s", "2s", "5s"];

    pub fn from_idx(idx: usize) -> Self {
        match idx {
            0 => Self::OneSec,
            1 => Self::TwoSec,
            _ => Self::FiveSec,
        }
    }

    pub fn to_idx(self) -> usize {
        match self {
            Self::OneSec => 0,
            Self::TwoSec => 1,
            Self::FiveSec => 2,
        }
    }

    pub fn as_duration(self) -> std::time::Duration {
        match self {
            Self::OneSec => std::time::Duration::from_secs(1),
            Self::TwoSec => std::time::Duration::from_secs(2),
            Self::FiveSec => std::time::Duration::from_secs(5),
        }
    }
}

// ─── Model preference ────────────────────────────────────────────────────────

/// Per-machine model preference (session-level override; coordinator.yml
/// remains the project-level source of truth).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum ModelPref {
    #[default]
    Sonnet,
    Opus,
    Haiku,
}

impl ModelPref {
    pub const LABELS: &'static [&'static str] = &["sonnet", "opus", "haiku"];

    pub fn from_idx(idx: usize) -> Self {
        match idx {
            0 => Self::Sonnet,
            1 => Self::Opus,
            _ => Self::Haiku,
        }
    }

    pub fn to_idx(self) -> usize {
        match self {
            Self::Sonnet => 0,
            Self::Opus => 1,
            Self::Haiku => 2,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Sonnet => "sonnet",
            Self::Opus => "opus",
            Self::Haiku => "haiku",
        }
    }
}

impl std::fmt::Display for ModelPref {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

// ─── Theme ───────────────────────────────────────────────────────────────────

/// Visual theme for the TUI.
///
/// Currently only `Dark` is fully styled; `Light` and `HighContrast` are
/// reserved for when the themes feature lands.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum Theme {
    #[default]
    Dark,
    Light,
    HighContrast,
}

impl Theme {
    pub const LABELS: &'static [&'static str] = &["Dark", "Light", "High Contrast"];

    pub fn from_idx(idx: usize) -> Self {
        match idx {
            0 => Self::Dark,
            1 => Self::Light,
            _ => Self::HighContrast,
        }
    }

    pub fn to_idx(self) -> usize {
        match self {
            Self::Dark => 0,
            Self::Light => 1,
            Self::HighContrast => 2,
        }
    }
}

// ─── TuiSettings ─────────────────────────────────────────────────────────────

/// All user-facing settings that are persisted to `~/.coord/settings.toml`.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct TuiSettings {
    /// Active UI theme.
    pub theme: Theme,

    /// How often the board data is reloaded from the SQLite database.
    pub refresh_cadence: RefreshCadence,

    /// Play a system beep (or notification) when an assignment completes.
    pub audio_on_completion: bool,

    /// How long a fetched log is cached before re-requesting from the agent.
    pub log_cache_ttl: LogCacheTtl,

    /// Session-level model overrides keyed by machine name.
    /// These do not modify `coordinator.yml`; they are passed to workers at
    /// dispatch time when the user explicitly overrides the default.
    #[serde(default)]
    pub machine_model: HashMap<String, ModelPref>,
}

impl Default for TuiSettings {
    fn default() -> Self {
        Self {
            theme: Theme::default(),
            refresh_cadence: RefreshCadence::default(),
            audio_on_completion: false,
            log_cache_ttl: LogCacheTtl::default(),
            machine_model: HashMap::new(),
        }
    }
}

impl TuiSettings {
    /// Return the path to the settings file (`~/.coord/settings.toml`).
    pub fn path() -> PathBuf {
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("/tmp"));
        home.join(".coord").join("settings.toml")
    }

    /// Load settings from disk.  If the file does not exist or cannot be
    /// parsed, default settings are returned and no error is surfaced — the
    /// first save will create a clean file.
    pub fn load() -> Self {
        let path = Self::path();
        let Ok(text) = std::fs::read_to_string(&path) else {
            return Self::default();
        };
        match toml::from_str::<Self>(&text) {
            Ok(s) => s,
            Err(_) => Self::default(),
        }
    }

    /// Persist settings to disk.  Silently ignores write errors — the TUI
    /// must remain usable even when the home directory is read-only.
    pub fn save(&self) {
        let path = Self::path();
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        if let Ok(text) = toml::to_string_pretty(self) {
            let _ = std::fs::write(&path, text);
        }
    }
}
