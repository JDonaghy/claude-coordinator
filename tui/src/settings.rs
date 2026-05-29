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

// ─── Keybindings ─────────────────────────────────────────────────────────────

/// User-mappable action names recognised by the TUI.
pub const ACTION_PIPELINE_REFRESH: &str = "pipeline_refresh";

/// Returns the default key string for a named action.
pub fn default_keybinding(action: &str) -> Option<&'static str> {
    match action {
        ACTION_PIPELINE_REFRESH => Some("Ctrl+R"),
        _ => None,
    }
}

/// Build the default keybindings map used when no `[keybindings]` section
/// exists in `settings.toml`.
pub fn default_keybindings() -> HashMap<String, String> {
    let mut m = HashMap::new();
    m.insert(ACTION_PIPELINE_REFRESH.to_string(), "Ctrl+R".to_string());
    m
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

    /// User-mappable key bindings.  Keys are action names (e.g.
    /// `"pipeline_refresh"`); values are key strings in either vim-style
    /// (`<C-r>`) or plus-style (`Ctrl+R`).  Unrecognised action names are
    /// silently ignored.  An action with an empty string disables the binding.
    #[serde(default = "default_keybindings")]
    pub keybindings: HashMap<String, String>,
}

impl Default for TuiSettings {
    fn default() -> Self {
        Self {
            theme: Theme::default(),
            refresh_cadence: RefreshCadence::default(),
            audio_on_completion: false,
            log_cache_ttl: LogCacheTtl::default(),
            machine_model: HashMap::new(),
            keybindings: default_keybindings(),
        }
    }
}

impl TuiSettings {
    /// Return the path to the settings file (`~/.coord/settings.toml`), or
    /// `None` when the `HOME` environment variable is not set.
    ///
    /// When `HOME` is absent, load and save are skipped entirely — we never
    /// fall back to `/tmp` because that risks leaking settings between users
    /// on a shared system.
    pub fn path() -> Option<PathBuf> {
        let home = std::env::var_os("HOME").map(PathBuf::from)?;
        Some(home.join(".coord").join("settings.toml"))
    }

    /// Load settings from a specific path.
    ///
    /// Returns defaults when the file does not exist or cannot be parsed —
    /// malformed TOML never causes a panic.  Used by [`load`] and in tests.
    pub fn load_from_path(path: &std::path::Path) -> Self {
        let Ok(text) = std::fs::read_to_string(path) else {
            return Self::default();
        };
        match toml::from_str::<Self>(&text) {
            Ok(s) => s,
            Err(_) => Self::default(),
        }
    }

    /// Load settings from disk.  Returns defaults when `HOME` is unset, the
    /// file does not exist, or the file cannot be parsed.
    pub fn load() -> Self {
        let Some(path) = Self::path() else {
            eprintln!("coord-tui: HOME not set — settings will not be persisted");
            return Self::default();
        };
        Self::load_from_path(&path)
    }

    /// Persist settings to a specific path.
    ///
    /// Creates parent directories as needed.  Returns an error string when
    /// the directory cannot be created or the file cannot be written.
    /// Used by [`save`] and in tests.
    pub fn save_to_path(&self, path: &std::path::Path) -> Result<(), String> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|e| format!("create settings dir: {e}"))?;
        }
        let text = toml::to_string_pretty(self)
            .map_err(|e| format!("serialize settings: {e}"))?;
        std::fs::write(path, text)
            .map_err(|e| format!("write settings: {e}"))?;
        Ok(())
    }

    /// Persist settings to `~/.coord/settings.toml`.
    ///
    /// Returns an error string when `HOME` is unset or the write fails.
    /// The caller is responsible for surfacing the error to the user.
    /// The TUI must remain functional even when the home directory is
    /// read-only, so errors should be shown as non-fatal toasts.
    pub fn save(&self) -> Result<(), String> {
        let Some(path) = Self::path() else {
            // HOME not set — skip save silently (already warned on load).
            return Ok(());
        };
        self.save_to_path(&path)
    }
}

// ─── Tests ───────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    // ── Enum round-trips ──────────────────────────────────────────────────────

    #[test]
    fn theme_round_trip_all_variants() {
        for v in [Theme::Dark, Theme::Light, Theme::HighContrast] {
            assert_eq!(Theme::from_idx(v.to_idx()), v, "round-trip failed for {v:?}");
        }
    }

    #[test]
    fn theme_from_idx_out_of_range_returns_high_contrast() {
        assert_eq!(Theme::from_idx(99), Theme::HighContrast);
        assert_eq!(Theme::from_idx(usize::MAX), Theme::HighContrast);
    }

    #[test]
    fn refresh_cadence_round_trip_all_variants() {
        for v in [
            RefreshCadence::OneSec,
            RefreshCadence::FiveSec,
            RefreshCadence::ThirtySec,
            RefreshCadence::Off,
        ] {
            assert_eq!(RefreshCadence::from_idx(v.to_idx()), v, "round-trip failed for {v:?}");
        }
    }

    #[test]
    fn refresh_cadence_from_idx_out_of_range_returns_off() {
        assert_eq!(RefreshCadence::from_idx(99), RefreshCadence::Off);
        assert_eq!(RefreshCadence::from_idx(usize::MAX), RefreshCadence::Off);
    }

    #[test]
    fn log_cache_ttl_round_trip_all_variants() {
        for v in [LogCacheTtl::OneSec, LogCacheTtl::TwoSec, LogCacheTtl::FiveSec] {
            assert_eq!(LogCacheTtl::from_idx(v.to_idx()), v, "round-trip failed for {v:?}");
        }
    }

    #[test]
    fn log_cache_ttl_from_idx_out_of_range_returns_five_sec() {
        assert_eq!(LogCacheTtl::from_idx(99), LogCacheTtl::FiveSec);
        assert_eq!(LogCacheTtl::from_idx(usize::MAX), LogCacheTtl::FiveSec);
    }

    #[test]
    fn model_pref_round_trip_all_variants() {
        for v in [ModelPref::Sonnet, ModelPref::Opus, ModelPref::Haiku] {
            assert_eq!(ModelPref::from_idx(v.to_idx()), v, "round-trip failed for {v:?}");
        }
    }

    #[test]
    fn model_pref_from_idx_out_of_range_returns_haiku() {
        assert_eq!(ModelPref::from_idx(99), ModelPref::Haiku);
        assert_eq!(ModelPref::from_idx(usize::MAX), ModelPref::Haiku);
    }

    // ── load_from_path with malformed TOML ────────────────────────────────────

    #[test]
    fn load_from_path_malformed_toml_returns_defaults() {
        let dir = std::env::temp_dir();
        let path = dir.join(format!("coord_settings_test_malformed_{}.toml", std::process::id()));
        std::fs::write(&path, b"this is [[[ not valid toml at all").unwrap();
        let s = TuiSettings::load_from_path(&path);
        let _ = std::fs::remove_file(&path);
        // Must return defaults without panicking.
        assert_eq!(s.theme, Theme::default());
        assert_eq!(s.refresh_cadence, RefreshCadence::default());
        assert!(!s.audio_on_completion);
        assert_eq!(s.log_cache_ttl, LogCacheTtl::default());
        assert!(s.machine_model.is_empty());
    }

    #[test]
    fn load_from_path_missing_file_returns_defaults() {
        let path = std::env::temp_dir().join("coord_settings_test_missing_9999999.toml");
        let s = TuiSettings::load_from_path(&path);
        assert_eq!(s.theme, Theme::default());
        assert!(s.machine_model.is_empty());
    }

    // ── TOML round-trip ───────────────────────────────────────────────────────

    #[test]
    fn save_to_path_then_load_from_path_preserves_all_fields() {
        use std::collections::HashMap;

        let dir = std::env::temp_dir();
        let path = dir.join(format!("coord_settings_test_roundtrip_{}.toml", std::process::id()));

        let mut machine_model = HashMap::new();
        machine_model.insert("mybox".to_string(), ModelPref::Opus);
        machine_model.insert("laptop".to_string(), ModelPref::Haiku);

        let original = TuiSettings {
            theme: Theme::Light,
            refresh_cadence: RefreshCadence::ThirtySec,
            audio_on_completion: true,
            log_cache_ttl: LogCacheTtl::FiveSec,
            machine_model,
            keybindings: default_keybindings(),
        };

        original.save_to_path(&path).expect("save should succeed");
        let loaded = TuiSettings::load_from_path(&path);
        let _ = std::fs::remove_file(&path);

        assert_eq!(loaded.theme, Theme::Light);
        assert_eq!(loaded.refresh_cadence, RefreshCadence::ThirtySec);
        assert!(loaded.audio_on_completion);
        assert_eq!(loaded.log_cache_ttl, LogCacheTtl::FiveSec);
        assert_eq!(loaded.machine_model.get("mybox"), Some(&ModelPref::Opus));
        assert_eq!(loaded.machine_model.get("laptop"), Some(&ModelPref::Haiku));
        assert_eq!(
            loaded.keybindings.get(ACTION_PIPELINE_REFRESH).map(|s| s.as_str()),
            Some("Ctrl+R"),
        );
    }

    #[test]
    fn default_keybindings_includes_pipeline_refresh() {
        let s = TuiSettings::default();
        assert_eq!(
            s.keybindings.get(ACTION_PIPELINE_REFRESH).map(|s| s.as_str()),
            Some("Ctrl+R"),
        );
    }

    #[test]
    fn keybinding_empty_string_disables_action() {
        let mut s = TuiSettings::default();
        s.keybindings.insert(ACTION_PIPELINE_REFRESH.to_string(), String::new());
        let dir = std::env::temp_dir();
        let path = dir.join(format!("coord_settings_test_empty_bind_{}.toml", std::process::id()));
        s.save_to_path(&path).expect("save");
        let loaded = TuiSettings::load_from_path(&path);
        let _ = std::fs::remove_file(&path);
        assert_eq!(
            loaded.keybindings.get(ACTION_PIPELINE_REFRESH).map(|s| s.as_str()),
            Some(""),
            "empty string binding should survive round-trip"
        );
    }

    #[test]
    fn save_to_path_creates_parent_dirs() {
        let dir = std::env::temp_dir()
            .join(format!("coord_test_mkdirs_{}", std::process::id()));
        let path = dir.join("nested").join("settings.toml");
        TuiSettings::default()
            .save_to_path(&path)
            .expect("should create parent dirs and succeed");
        assert!(path.exists());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
