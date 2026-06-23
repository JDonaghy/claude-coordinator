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
/// Each variant maps to a full [`quadraui::Theme`] colour palette via
/// [`to_quadraui_theme`]. The `Dark` palette matches the pre-theming
/// hardcoded colours so existing users see no visual change on upgrade.
///
/// [`to_quadraui_theme`]: Theme::to_quadraui_theme
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum Theme {
    #[default]
    Dark,
    Light,
    HighContrast,
    Solarized,
}

impl Theme {
    pub const LABELS: &'static [&'static str] =
        &["Dark", "Light", "High Contrast", "Solarized"];

    pub fn from_idx(idx: usize) -> Self {
        match idx {
            0 => Self::Dark,
            1 => Self::Light,
            2 => Self::HighContrast,
            _ => Self::Solarized,
        }
    }

    pub fn to_idx(self) -> usize {
        match self {
            Self::Dark => 0,
            Self::Light => 1,
            Self::HighContrast => 2,
            Self::Solarized => 3,
        }
    }

    /// Convert the selected theme variant into a full [`quadraui::Theme`]
    /// colour palette.
    ///
    /// The returned palette drives status badges, markdown rendering, and
    /// all other theme-sensitive rendering in `CoordApp`. Computed once at
    /// startup and again whenever the settings panel changes the selection;
    /// cached in `CoordApp::active_theme`.
    pub fn to_quadraui_theme(self) -> quadraui::Theme {
        match self {
            Theme::Dark => dark_palette(),
            Theme::Light => light_palette(),
            Theme::HighContrast => high_contrast_palette(),
            Theme::Solarized => solarized_palette(),
        }
    }
}

// ─── Built-in palettes ────────────────────────────────────────────────────────

/// Dark palette — tuned to match coord-tui's pre-theming hardcoded colours so
/// existing users see no visual change after the theming migration.
fn dark_palette() -> quadraui::Theme {
    use quadraui::Color;
    quadraui::Theme {
        // ── Status-badge overrides (coord-tui semantics differ from quadraui
        //    defaults: "running" is green here, not yellow) ──────────────────
        badge_running: Color::rgb(80, 220, 80),          // active/running  = green
        badge_blocked: Color::rgb(220, 70, 70),          // failed          = red
        warning_fg: Color::rgb(200, 200, 70),            // pending/unknown  = yellow
        // ── Stage-badge overrides ─────────────────────────────────────────
        link_fg: Color::rgb(150, 200, 240),              // "work" stage
        badge_request_changes: Color::rgb(200, 180, 100),// "review" stage
        diagnostic_hint: Color::rgb(180, 150, 220),      // "smoke" stage
        accent_fg: Color::rgb(100, 180, 240),            // "merge" stage / key accent
        // badge_passed default (120,200,120) covers the "done" stage badge.
        // All other fields use quadraui's built-in dark defaults.
        ..quadraui::Theme::default()
    }
}

/// Light palette — dark text on a pale background.
///
/// Only the fields consumed by coord-tui's rendering are fully specified;
/// the `..dark_palette()` spread fills editor-specific fields that are
/// unused in the coordinator dashboard.
fn light_palette() -> quadraui::Theme {
    use quadraui::Color;
    let bg = Color::rgb(245, 245, 240);
    let fg = Color::rgb(30, 30, 30);
    let muted = Color::rgb(105, 105, 115);
    quadraui::Theme {
        background: bg,
        foreground: fg,
        tab_bar_bg: Color::rgb(228, 230, 238),
        tab_active_bg: Color::rgb(255, 255, 255),
        tab_active_fg: fg,
        tab_inactive_fg: muted,
        tab_preview_active_fg: Color::rgb(60, 60, 80),
        tab_preview_inactive_fg: Color::rgb(130, 130, 145),
        separator: Color::rgb(185, 188, 200),
        surface_bg: Color::rgb(255, 255, 255),
        surface_fg: fg,
        selected_bg: Color::rgb(175, 210, 255),
        inactive_selected_bg: Color::rgb(210, 225, 248),
        border_fg: Color::rgb(100, 130, 185),
        title_fg: Color::rgb(40, 70, 145),
        header_bg: Color::rgb(225, 228, 240),
        header_fg: fg,
        muted_fg: muted,
        error_fg: Color::rgb(185, 40, 40),
        warning_fg: Color::rgb(140, 110, 0),
        query_fg: fg,
        match_fg: Color::rgb(180, 100, 0),
        accent_fg: Color::rgb(50, 100, 185),
        hover_bg: Color::rgb(238, 240, 250),
        hover_fg: fg,
        hover_border: Color::rgb(100, 130, 185),
        input_bg: Color::rgb(248, 248, 252),
        inactive_fg: muted,
        selection_bg: Color::rgb(175, 200, 235),
        link_fg: Color::rgb(50, 105, 205),              // "work" stage
        completion_bg: Color::rgb(250, 250, 252),
        completion_fg: fg,
        completion_border: Color::rgb(100, 130, 185),
        completion_selected_bg: Color::rgb(175, 210, 255),
        accent_bg: Color::rgb(50, 100, 185),
        scrollbar_track: Color::rgb(210, 213, 220),
        scrollbar_thumb: Color::rgb(150, 156, 172),
        // ── Status badges ─────────────────────────────────────────────────
        badge_running: Color::rgb(40, 160, 40),          // active = darker green
        badge_passed: Color::rgb(40, 140, 40),           // done   = dark green
        badge_blocked: Color::rgb(185, 40, 40),          // failed = dark red
        badge_request_changes: Color::rgb(155, 120, 0),  // review = dark amber
        diagnostic_hint: Color::rgb(110, 80, 180),       // smoke  = dark violet
        // ── Board ─────────────────────────────────────────────────────────
        board_selected_card_bg: Color::rgb(175, 210, 255),
        board_col_header_bg: Color::rgb(225, 228, 240),
        decision_hint_bg: Color::rgb(228, 232, 252),
        decision_hint_fg: Color::rgb(40, 60, 125),
        // Editor fields unused in coord-tui — light-appropriate defaults.
        editor_active_background: Color::rgb(255, 255, 255),
        cursorline_bg: Color::rgb(240, 243, 250),
        dap_stopped_bg: Color::rgb(255, 242, 185),
        colorcolumn_bg: Color::rgb(238, 240, 250),
        diff_added_bg: Color::rgb(210, 242, 210),
        diff_removed_bg: Color::rgb(255, 215, 215),
        diff_padding_bg: Color::rgb(240, 243, 250),
        line_number_fg: muted,
        line_number_active_fg: fg,
        diagnostic_error: Color::rgb(185, 40, 40),
        diagnostic_warning: Color::rgb(140, 110, 0),
        diagnostic_info: Color::rgb(50, 100, 185),
        git_added: Color::rgb(40, 140, 40),
        git_modified: Color::rgb(140, 110, 0),
        git_deleted: Color::rgb(185, 40, 40),
        lightbulb: Color::rgb(200, 160, 0),
        spell_error: Color::rgb(0, 150, 150),
        cursor: fg,
        cursor_normal_alpha: 0.40,
        selection: Color::rgb(175, 200, 235),
        selection_alpha: 0.50,
        yank_highlight_bg: Color::rgb(255, 232, 100),
        yank_highlight_alpha: 0.40,
        bracket_match_bg: Color::rgb(200, 218, 238),
        indent_guide_fg: Color::rgb(200, 203, 212),
        indent_guide_active_fg: Color::rgb(150, 157, 172),
        annotation_fg: muted,
        ghost_text_fg: Color::rgb(160, 165, 180),
        command_line_bg: bg,
        command_line_fg: fg,
    }
}

/// High-contrast palette — maximum legibility on black.
fn high_contrast_palette() -> quadraui::Theme {
    use quadraui::Color;
    let bg = Color::rgb(0, 0, 0);
    let fg = Color::rgb(255, 255, 255);
    quadraui::Theme {
        background: bg,
        foreground: fg,
        tab_bar_bg: bg,
        tab_active_bg: Color::rgb(30, 30, 30),
        tab_active_fg: fg,
        tab_inactive_fg: Color::rgb(160, 160, 160),
        tab_preview_active_fg: Color::rgb(200, 200, 200),
        tab_preview_inactive_fg: Color::rgb(130, 130, 130),
        separator: Color::rgb(80, 80, 80),
        surface_bg: Color::rgb(15, 15, 15),
        surface_fg: fg,
        selected_bg: Color::rgb(0, 80, 180),
        inactive_selected_bg: Color::rgb(30, 50, 100),
        border_fg: Color::rgb(255, 255, 255),
        title_fg: Color::rgb(255, 255, 255),
        header_bg: Color::rgb(30, 30, 30),
        header_fg: fg,
        muted_fg: Color::rgb(160, 160, 160),
        error_fg: Color::rgb(255, 60, 60),
        warning_fg: Color::rgb(255, 255, 0),
        query_fg: fg,
        match_fg: Color::rgb(255, 220, 0),
        accent_fg: Color::rgb(0, 200, 255),
        hover_bg: Color::rgb(20, 20, 20),
        hover_fg: fg,
        hover_border: Color::rgb(200, 200, 200),
        input_bg: Color::rgb(20, 20, 20),
        inactive_fg: Color::rgb(140, 140, 140),
        selection_bg: Color::rgb(0, 80, 180),
        link_fg: Color::rgb(100, 180, 255),              // "work" stage
        completion_bg: Color::rgb(15, 15, 15),
        completion_fg: fg,
        completion_border: fg,
        completion_selected_bg: Color::rgb(0, 80, 180),
        accent_bg: Color::rgb(0, 120, 220),
        scrollbar_track: Color::rgb(30, 30, 30),
        scrollbar_thumb: Color::rgb(140, 140, 140),
        // ── Status badges ─────────────────────────────────────────────────
        badge_running: Color::rgb(0, 255, 0),            // active = pure green
        badge_passed: Color::rgb(0, 220, 0),             // done   = bright green
        badge_blocked: Color::rgb(255, 0, 0),            // failed = pure red
        badge_request_changes: Color::rgb(255, 180, 0),  // review = bright amber
        diagnostic_hint: Color::rgb(200, 100, 255),      // smoke  = bright violet
        // ── Board ─────────────────────────────────────────────────────────
        board_selected_card_bg: Color::rgb(0, 80, 180),
        board_col_header_bg: Color::rgb(30, 30, 30),
        decision_hint_bg: Color::rgb(20, 20, 20),
        decision_hint_fg: fg,
        // Editor fields
        editor_active_background: bg,
        cursorline_bg: Color::rgb(20, 20, 20),
        dap_stopped_bg: Color::rgb(80, 60, 0),
        colorcolumn_bg: Color::rgb(20, 20, 20),
        diff_added_bg: Color::rgb(0, 60, 0),
        diff_removed_bg: Color::rgb(80, 0, 0),
        diff_padding_bg: Color::rgb(15, 15, 15),
        line_number_fg: Color::rgb(140, 140, 140),
        line_number_active_fg: fg,
        diagnostic_error: Color::rgb(255, 60, 60),
        diagnostic_warning: Color::rgb(255, 200, 0),
        diagnostic_info: Color::rgb(0, 200, 255),
        git_added: Color::rgb(0, 220, 0),
        git_modified: Color::rgb(255, 200, 0),
        git_deleted: Color::rgb(255, 60, 60),
        lightbulb: Color::rgb(255, 220, 0),
        spell_error: Color::rgb(0, 220, 220),
        cursor: fg,
        cursor_normal_alpha: 0.60,
        selection: Color::rgb(0, 80, 180),
        selection_alpha: 0.60,
        yank_highlight_bg: Color::rgb(255, 220, 0),
        yank_highlight_alpha: 0.40,
        bracket_match_bg: Color::rgb(60, 60, 80),
        indent_guide_fg: Color::rgb(60, 60, 60),
        indent_guide_active_fg: Color::rgb(140, 140, 140),
        annotation_fg: Color::rgb(140, 140, 140),
        ghost_text_fg: Color::rgb(120, 120, 120),
        command_line_bg: bg,
        command_line_fg: fg,
    }
}

/// Solarized Dark palette — uses the canonical Solarized colour assignments.
///
/// Reference: <https://ethanschoonover.com/solarized/>
fn solarized_palette() -> quadraui::Theme {
    use quadraui::Color;
    // Solarized base tones
    let base03 = Color::rgb(0, 43, 54);      // background (darkest)
    let base02 = Color::rgb(7, 54, 66);      // background highlights
    let base01 = Color::rgb(88, 110, 117);   // secondary content / muted
    let base0 = Color::rgb(131, 148, 150);   // body text
    let base1 = Color::rgb(147, 161, 161);   // optional emphasis
    // Solarized accent tones
    let yellow = Color::rgb(181, 137, 0);
    let orange = Color::rgb(203, 75, 22);
    let red = Color::rgb(220, 50, 47);
    let violet = Color::rgb(108, 113, 196);
    let blue = Color::rgb(38, 139, 210);
    let cyan = Color::rgb(42, 161, 152);
    let green = Color::rgb(133, 153, 0);
    quadraui::Theme {
        background: base03,
        foreground: base0,
        tab_bar_bg: base03,
        tab_active_bg: base02,
        tab_active_fg: base1,
        tab_inactive_fg: base01,
        tab_preview_active_fg: base0,
        tab_preview_inactive_fg: base01,
        separator: base02,
        surface_bg: base02,
        surface_fg: base0,
        selected_bg: Color::rgb(0, 75, 90),
        inactive_selected_bg: Color::rgb(10, 58, 70),
        border_fg: cyan,
        title_fg: base1,
        header_bg: base02,
        header_fg: base0,
        muted_fg: base01,
        error_fg: red,
        warning_fg: yellow,
        query_fg: base0,
        match_fg: yellow,
        accent_fg: blue,
        hover_bg: base02,
        hover_fg: base0,
        hover_border: cyan,
        input_bg: Color::rgb(5, 50, 62),
        inactive_fg: base01,
        selection_bg: Color::rgb(0, 75, 90),
        link_fg: cyan,                                   // "work" stage
        completion_bg: base02,
        completion_fg: base0,
        completion_border: cyan,
        completion_selected_bg: Color::rgb(0, 75, 90),
        accent_bg: blue,
        scrollbar_track: base02,
        scrollbar_thumb: base01,
        // ── Status badges ─────────────────────────────────────────────────
        badge_running: green,                            // active = solarized green
        badge_passed: Color::rgb(100, 140, 40),          // done   = muted green
        badge_blocked: red,                             // failed = solarized red
        badge_request_changes: orange,                  // review = solarized orange
        diagnostic_hint: violet,                        // smoke  = solarized violet
        // ── Board ─────────────────────────────────────────────────────────
        board_selected_card_bg: Color::rgb(0, 75, 90),
        board_col_header_bg: base02,
        decision_hint_bg: Color::rgb(5, 50, 62),
        decision_hint_fg: base1,
        // Editor fields
        editor_active_background: base03,
        cursorline_bg: Color::rgb(5, 50, 62),
        dap_stopped_bg: Color::rgb(60, 50, 0),
        colorcolumn_bg: Color::rgb(5, 50, 62),
        diff_added_bg: Color::rgb(20, 55, 30),
        diff_removed_bg: Color::rgb(55, 20, 20),
        diff_padding_bg: base02,
        line_number_fg: base01,
        line_number_active_fg: base0,
        diagnostic_error: red,
        diagnostic_warning: yellow,
        diagnostic_info: blue,
        git_added: green,
        git_modified: yellow,
        git_deleted: red,
        lightbulb: yellow,
        spell_error: cyan,
        cursor: base0,
        cursor_normal_alpha: 0.40,
        selection: Color::rgb(0, 75, 90),
        selection_alpha: 0.50,
        yank_highlight_bg: yellow,
        yank_highlight_alpha: 0.30,
        bracket_match_bg: Color::rgb(30, 70, 82),
        indent_guide_fg: base02,
        indent_guide_active_fg: base01,
        annotation_fg: base01,
        ghost_text_fg: base01,
        command_line_bg: base03,
        command_line_fg: base0,
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

    /// Load a fully-custom colour palette from `~/.coord/theme.toml`, if
    /// present.
    ///
    /// The file must be valid TOML that deserialises to [`quadraui::Theme`].
    /// When the file is absent or malformed the function returns `None` and
    /// the caller falls back to the built-in palette selected by
    /// [`TuiSettings::theme`].
    ///
    /// Tip: start from a variant's palette (e.g. `coord-tui --dump-theme
    /// dark > ~/.coord/theme.toml`) and edit individual fields — the
    /// remaining fields keep their built-in values.
    pub fn load_custom_theme_file() -> Option<quadraui::Theme> {
        let home = std::env::var_os("HOME").map(PathBuf::from)?;
        let path = home.join(".coord").join("theme.toml");
        let text = std::fs::read_to_string(&path).ok()?;
        toml::from_str::<quadraui::Theme>(&text).ok()
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
        for v in [Theme::Dark, Theme::Light, Theme::HighContrast, Theme::Solarized] {
            assert_eq!(Theme::from_idx(v.to_idx()), v, "round-trip failed for {v:?}");
        }
    }

    #[test]
    fn theme_from_idx_out_of_range_returns_solarized() {
        // Solarized is the last/catch-all variant; out-of-range falls through to it.
        assert_eq!(Theme::from_idx(99), Theme::Solarized);
        assert_eq!(Theme::from_idx(usize::MAX), Theme::Solarized);
    }

    #[test]
    fn theme_to_quadraui_theme_returns_distinct_palettes() {
        // Smoke test: each variant produces a palette whose background differs
        // from the others (a proxy for "four distinct palettes were defined").
        let dark = Theme::Dark.to_quadraui_theme();
        let light = Theme::Light.to_quadraui_theme();
        let hc = Theme::HighContrast.to_quadraui_theme();
        let sol = Theme::Solarized.to_quadraui_theme();
        assert_ne!(dark.background, light.background, "Dark vs Light must differ");
        assert_ne!(dark.background, hc.background, "Dark vs HighContrast must differ");
        assert_ne!(dark.background, sol.background, "Dark vs Solarized must differ");
        assert_ne!(light.background, hc.background, "Light vs HighContrast must differ");
    }

    #[test]
    fn dark_palette_badge_running_is_green() {
        // The Dark palette must override badge_running to green (80,220,80) so the
        // assignment-status colour matches the pre-theming hardcoded value.
        use quadraui::Color;
        let t = Theme::Dark.to_quadraui_theme();
        assert_eq!(t.badge_running, Color::rgb(80, 220, 80));
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

        assert_eq!(loaded.theme, Theme::Light, "theme should survive TOML round-trip");
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
