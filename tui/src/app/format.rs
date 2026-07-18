//! Pure formatting helpers extracted from `app/mod.rs` (#743).
//!
//! No I/O, no quadraui types, no app state — pure text/number transformations.
use std::time::{SystemTime, UNIX_EPOCH};


pub(crate) fn fmt_dur(secs: u64) -> String {
    if secs < 60 {
        format!("{}s", secs)
    } else if secs < 3600 {
        format!("{}m", secs / 60)
    } else {
        format!("{}h{}m", secs / 3600, (secs % 3600) / 60)
    }
}

/// Format elapsed seconds as mm:ss (under 1 hour) or h:mm (≥1 hour).
pub(crate) fn fmt_elapsed_mmss(secs: u64) -> String {
    if secs < 3600 {
        format!("{}:{:02}", secs / 60, secs % 60)
    } else {
        format!("{}:{:02}", secs / 3600, (secs % 3600) / 60)
    }
}

/// Truncate `s` to at most `max_chars` Unicode scalar values.
/// Capitalize the first ASCII character of `s` (no-op when `s` is empty
/// or starts with a non-ASCII character).
pub(crate) fn capitalize(s: &str) -> String {
    let mut out = s.to_string();
    if let Some(c) = out.get_mut(0..1) {
        c.make_ascii_uppercase();
    }
    out
}

/// Format a unix timestamp as a relative "Xs/m/h ago" string using
/// the existing `fmt_dur` helper.  Falls back to "-" when the
/// timestamp is in the future or the system clock can't be read.
/// (#818: previously used by the Stages tab detail rows; #1022: now
/// used by the Pipeline Summary tab to show relative completion times.)
pub(crate) fn format_unix_time(ts: f64) -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    let delta = (now - ts).max(0.0) as u64;
    format!("{} ago", fmt_dur(delta))
}

/// #546: format a token count with K/M suffix and one decimal place.
///
/// Examples: 1500 → "1.5k", 2_300_000 → "2.3M", 800 → "800".
/// Used to keep token counts readable in the narrow TUI columns.
pub(crate) fn fmt_tokens(n: i64) -> String {
    if n <= 0 {
        return "0".to_string();
    }
    if n >= 1_000_000 {
        format!("{:.1}M", n as f64 / 1_000_000.0)
    } else if n >= 1_000 {
        format!("{:.1}k", n as f64 / 1_000.0)
    } else {
        n.to_string()
    }
}

/// #208: format a worker cost in USD with two decimals.  Below 1¢ shows
/// "< $0.01" so the rendering doesn't read as $0.00 (mathematically true
/// but misleading — the worker did some non-zero work).
pub(crate) fn format_cost_usd(cost: f64) -> String {
    if cost <= 0.0 {
        "$0.00".to_string()
    } else if cost < 0.01 {
        "< $0.01".to_string()
    } else {
        format!("${cost:.2}")
    }
}

/// #1116: format an ESTIMATED worker cost in USD, visually distinct from a
/// captured figure (`format_cost_usd`) via a `~$` prefix — matches the CLI's
/// (#1115) `~$` convention so an interactive-heavy issue never reads as
/// "no cost data" just because nothing was captured. Zero renders as "—"
/// (not "~$0.00") since "no estimate" and "estimated zero" are the same
/// thing here — unlike captured cost, there's no ambiguity to flag.
pub(crate) fn format_cost_est(cost: f64) -> String {
    if cost <= 0.0 {
        "—".to_string()
    } else if cost < 0.01 {
        "~< $0.01".to_string()
    } else {
        format!("~${cost:.2}")
    }
}

/// #1116: render a captured cost for the Usage grid/drill, where a genuine
/// zero (no captured cost at all — e.g. an interactive-only leg) should read
/// as "—" rather than `format_cost_usd`'s "$0.00" (which elsewhere means
/// "captured, and it rounds to zero"). The Usage view always has cost_est
/// as a companion column, so "—" here is unambiguous: nothing was captured.
pub(crate) fn format_cost_captured(cost: f64) -> String {
    if cost <= 0.0 {
        "—".to_string()
    } else {
        format_cost_usd(cost)
    }
}

/// #1116: compact `NmSSs` duration for the Usage grid/drill (e.g.
/// `"45m00s"`), distinct from `fmt_dur`'s coarser `"45m"`/`"1h30m"` (used
/// elsewhere for elapsed-time display) — the Usage view sums durations
/// across many legs, so seconds-precision avoids "0m" reading as "no data"
/// for a short leg. `secs <= 0.0` (no duration recorded, or a still-open
/// leg with nothing finished yet) renders as "—".
pub(crate) fn format_duration_usage(secs: f64) -> String {
    if secs <= 0.0 {
        return "—".to_string();
    }
    let total = secs.round() as u64;
    let (h, rem) = (total / 3600, total % 3600);
    let (m, s) = (rem / 60, rem % 60);
    if h > 0 {
        format!("{h}h{m:02}m{s:02}s")
    } else {
        format!("{m}m{s:02}s")
    }
}

/// Collapse all runs of whitespace (including newlines and tabs) into single
/// spaces and trim the ends. Used to render a multi-line assistant text block
/// as one horizontally-scrollable Log row (#302) without embedded newlines
/// breaking the single-line list item.
pub(crate) fn collapse_ws(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

pub(crate) fn trunc(s: &str, max_chars: usize) -> &str {
    match s.char_indices().nth(max_chars) {
        Some((byte_idx, _)) => &s[..byte_idx],
        None => s,
    }
}

/// #541: Fuzzy subsequence match scorer used by the global issue finder.
///
/// Returns `None` when not every character of `query` appears in `haystack`
/// in order (case-insensitive).  Returns `Some((score, positions))` otherwise,
/// where `positions` is the list of matched character indices in `haystack`
/// (char indices, not byte offsets), each matched character contributes +1 to
/// score, and each *consecutive* match pair earns an additional +2 bonus so
/// tighter matches rank higher.  An empty query always matches with score 0
/// and an empty positions list.
pub(crate) fn fuzzy_score(query: &str, haystack: &str) -> Option<(u32, Vec<usize>)> {
    if query.is_empty() {
        return Some((0, Vec::new()));
    }
    let q: Vec<char> = query.to_lowercase().chars().collect();
    let h: Vec<char> = haystack.to_lowercase().chars().collect();
    let mut qi = 0usize;
    let mut score: u32 = 0;
    let mut prev_matched = false;
    let mut positions: Vec<usize> = Vec::new();
    for (hi, hc) in h.iter().enumerate() {
        if qi < q.len() && *hc == q[qi] {
            qi += 1;
            score += 1;
            if prev_matched {
                score += 2; // consecutive-run bonus
            }
            prev_matched = true;
            positions.push(hi);
        } else {
            prev_matched = false;
        }
    }
    if qi == q.len() { Some((score, positions)) } else { None }
}

/// Word-wrap `text` to `width` character columns, respecting existing `\n`
/// line breaks.  Empty lines are preserved.  If `width == 0` or a line is
/// already within budget, it is emitted as-is.  Very long single "words"
/// (e.g. long URLs) are hard-broken at the column limit.
pub(crate) fn word_wrap(text: &str, width: usize) -> Vec<String> {
    let mut result = Vec::new();
    for line in text.lines() {
        if line.is_empty() {
            result.push(String::new());
            continue;
        }
        if width == 0 || line.chars().count() <= width {
            result.push(line.to_string());
            continue;
        }
        // Word-wrap this line.
        let mut current = String::new();
        for word in line.split(' ') {
            if word.is_empty() {
                // Preserve a single space gap when we still have room.
                if !current.is_empty() && current.chars().count() < width {
                    current.push(' ');
                }
                continue;
            }
            let word_len = word.chars().count();
            if current.is_empty() {
                if word_len > width {
                    // Hard-break a very long word at the column limit.
                    let mut rest = word;
                    while rest.chars().count() > width {
                        let cut = rest
                            .char_indices()
                            .nth(width)
                            .map(|(i, _)| i)
                            .unwrap_or(rest.len());
                        result.push(rest[..cut].to_string());
                        rest = &rest[cut..];
                    }
                    current = rest.to_string();
                } else {
                    current.push_str(word);
                }
            } else if current.chars().count() + 1 + word_len <= width {
                current.push(' ');
                current.push_str(word);
            } else {
                result.push(current);
                current = word.to_string();
            }
        }
        if !current.is_empty() {
            result.push(current);
        }
    }
    result
}
