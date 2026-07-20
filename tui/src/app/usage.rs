//! Usage ActivityBar panel (#1116).
//!
//! Per-issue (or per-repo) cost/token grid sourced entirely from the
//! already-loaded board assignments (`self.data.assignments` — no new
//! daemon round-trip, no local-log parsing, per the issue's Requirement 4).
//! The aggregation rules below are a Rust port of `coord/usage_rollup.py`
//! (#1118 Core / #1115 CLI-1), kept conceptually in sync: same window
//! predicate (`dispatched_at` OR `finished_at` in a half-open `[start,
//! end)` interval), same "captured cost wins, else estimate from tokens,
//! else flag unknown-model" leg-cost rule, same default sort (desc by
//! total cost = captured + est).
//!
//! **Pricing is a hardcoded snapshot** of `coord.config.PricingConfig`'s
//! shipped defaults (sonnet/opus/haiku per-1M-token rates) — the `/board`
//! payload carries only captured `cost_usd` + raw token counts, never the
//! rate table itself (`coord/serve_app.py`'s `/board` schema has no
//! `pricing` key anywhere), and the issue explicitly rules out a new
//! daemon round-trip just to fetch `coordinator.yml`'s `pricing:` block.
//! If an operator overrides pricing there, this view's `~$` estimates will
//! silently diverge from `coord usage`'s captured-vs-CLI-estimate figures —
//! recorded as a durable finding on issue #1116.
//!
//! **No chrono/time crate in this workspace** (`tui/Cargo.toml`; see also
//! `AuditTimeRange`, `types.rs`) — Today/Week/Month window boundaries are
//! computed with a small dependency-free civil-calendar algorithm (Howard
//! Hinnant's `civil_from_days`/`days_from_civil`,
//! <http://howardhinnant.github.io/date_algorithms.html>), UTC only.
//!
//! **Scope of this slice**: per-issue drill-down (click a row → per-stage
//! legs) is wired for `UsageGroupBy::Issue` only — a `Repo`-grouped row
//! selects but does not expand (no single issue to drill into). Sortable
//! columns are click-to-toggle but there's no column resize / scrollbar
//! drag (deferred, same as Audit's #1039 v1 before #1094 added those).
#[allow(unused_imports)]
use super::*;

// ── Civil-calendar math (no chrono/time crate available) ────────────────────

/// Days since the Unix epoch (1970-01-01) for a proleptic-Gregorian civil
/// date. Howard Hinnant's `days_from_civil` — pure integer arithmetic.
/// `div_euclid` (not `/`) is required for the `era` step: `y`/`z` can be
/// negative there and Rust's `/` truncates toward zero, but the algorithm
/// needs floor division (`div_euclid` on a positive divisor is exactly
/// floor division).
fn days_from_civil(y: i64, m: u32, d: u32) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 }.div_euclid(400);
    let yoe = y - era * 400; // [0, 399]
    let mp = (i64::from(m) + 9) % 12; // [0, 11]: Mar=0 .. Feb=11
    let doy = (153 * mp + 2) / 5 + i64::from(d) - 1; // [0, 365]
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy; // [0, 146096]
    era * 146_097 + doe - 719_468
}

/// Inverse of [`days_from_civil`]: days-since-epoch -> (year, month, day).
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 }.div_euclid(146_097);
    let doe = z - era * 146_097; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32; // [1, 12]
    let y = if m <= 2 { y + 1 } else { y };
    (y, m, d)
}

const SECS_PER_DAY: f64 = 86_400.0;

fn utc_day_start(now: f64) -> f64 {
    (now / SECS_PER_DAY).floor() * SECS_PER_DAY
}

/// Start of the UTC ISO week (Monday 00:00) containing `now`. 1970-01-01
/// (epoch day 0) was a Thursday, so `weekday = (epoch_days + 3).rem_euclid(7)`
/// gives days-since-Monday for any epoch day (Monday=0 .. Sunday=6).
fn utc_week_start(now: f64) -> f64 {
    let epoch_days = (utc_day_start(now) / SECS_PER_DAY).round() as i64;
    let weekday = (epoch_days + 3).rem_euclid(7);
    (epoch_days - weekday) as f64 * SECS_PER_DAY
}

/// `[start, end)` of the UTC calendar month containing `now`.
fn utc_month_bounds(now: f64) -> (f64, f64) {
    let epoch_days = (utc_day_start(now) / SECS_PER_DAY).round() as i64;
    let (y, m, _d) = civil_from_days(epoch_days);
    let start_days = days_from_civil(y, m, 1);
    let (next_y, next_m) = if m == 12 { (y + 1, 1) } else { (y, m + 1) };
    let end_days = days_from_civil(next_y, next_m, 1);
    (start_days as f64 * SECS_PER_DAY, end_days as f64 * SECS_PER_DAY)
}

/// Parse `"YYYY-MM-DD"` or `"YYYY-MM-DD HH:MM"` (a `T` separator is also
/// accepted) as a UTC instant (Unix seconds). Returns `None` for anything
/// else — there's no crate-provided date parser in this workspace.
pub(crate) fn parse_datetime_utc(s: &str) -> Option<f64> {
    let s = s.trim();
    let (date_part, time_part) = match s.split_once([' ', 'T']) {
        Some((d, t)) => (d, Some(t)),
        None => (s, None),
    };
    let mut date_fields = date_part.splitn(4, '-');
    let y: i64 = date_fields.next()?.parse().ok()?;
    let m: u32 = date_fields.next()?.parse().ok()?;
    let d: u32 = date_fields.next()?.parse().ok()?;
    if date_fields.next().is_some() || !(1..=12).contains(&m) || !(1..=31).contains(&d) {
        return None;
    }
    let (hh, mm): (u32, u32) = match time_part {
        Some(t) if !t.trim().is_empty() => {
            let mut fields = t.trim().splitn(3, ':');
            let hh: u32 = fields.next()?.parse().ok()?;
            let mm: u32 = match fields.next() {
                Some(field) => field.parse().ok()?,
                None => 0,
            };
            if fields.next().is_some() || hh > 23 || mm > 59 {
                return None;
            }
            (hh, mm)
        }
        _ => (0, 0),
    };
    let days = days_from_civil(y, m, d);
    Some(days as f64 * SECS_PER_DAY + f64::from(hh) * 3600.0 + f64::from(mm) * 60.0)
}

/// Render a UTC instant as `"YYYY-MM-DD HH:MM"` — the resolved custom-range
/// label shown in the sidebar/status-bar hint.
pub(crate) fn format_civil_datetime(ts: f64) -> String {
    let days = (ts / SECS_PER_DAY).floor() as i64;
    let secs_of_day = (ts - days as f64 * SECS_PER_DAY).max(0.0);
    let (y, m, d) = civil_from_days(days);
    let hh = (secs_of_day / 3600.0).floor() as u32;
    let mm = ((secs_of_day - f64::from(hh) * 3600.0) / 60.0).floor() as u32;
    format!("{y:04}-{m:02}-{d:02} {hh:02}:{mm:02}")
}

/// Current Unix time, seconds. Small wrapper so `usage`'s render/interaction
/// methods don't each repeat the `SystemTime` dance (mirrors
/// `format::format_unix_time`'s inline equivalent).
pub(crate) fn unix_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

// ── Time window ──────────────────────────────────────────────────────────────

/// A resolved half-open `[start, end)` interval, Unix seconds — the window a
/// leg is checked against. Mirrors `coord.usage_rollup.TimeWindow`.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct UsageWindow {
    pub(crate) start: Option<f64>,
    pub(crate) end: Option<f64>,
}

impl UsageWindow {
    /// Whether `ts` falls in `[start, end)`. `None` is never in-window.
    pub(crate) fn contains(&self, ts: Option<f64>) -> bool {
        let Some(ts) = ts else { return false };
        if let Some(start) = self.start {
            if ts < start {
                return false;
            }
        }
        if let Some(end) = self.end {
            if ts >= end {
                return false;
            }
        }
        true
    }
}

impl UsageScope {
    /// Resolve this scope to a concrete `[start, end)` window, anchored at
    /// `now` (Unix seconds) for the `Today`/`Week`/`Month` presets.
    pub(crate) fn resolve(self, now: f64) -> UsageWindow {
        match self {
            UsageScope::Today => {
                let start = utc_day_start(now);
                UsageWindow { start: Some(start), end: Some(start + SECS_PER_DAY) }
            }
            UsageScope::Week => {
                let start = utc_week_start(now);
                UsageWindow { start: Some(start), end: Some(start + 7.0 * SECS_PER_DAY) }
            }
            UsageScope::Month => {
                let (start, end) = utc_month_bounds(now);
                UsageWindow { start: Some(start), end: Some(end) }
            }
            UsageScope::Custom { start, end } => {
                UsageWindow { start: Some(start), end: Some(end) }
            }
        }
    }

    /// Short label for the sidebar/status-bar ("Today"/"Week"/"Month", or
    /// the resolved `"start → end"` instants for a custom range).
    pub(crate) fn label(self) -> String {
        match self {
            UsageScope::Today => "Today".to_string(),
            UsageScope::Week => "Week".to_string(),
            UsageScope::Month => "Month".to_string(),
            UsageScope::Custom { start, end } => {
                format!("{} → {}", format_civil_datetime(start), format_civil_datetime(end))
            }
        }
    }
}

// ── Model normalization + pricing snapshot ───────────────────────────────────

const KNOWN_MODELS: [&str; 3] = ["sonnet", "opus", "haiku"];
pub(crate) const UNKNOWN_MODEL: &str = "(unknown)";

/// Normalize a raw `model` field to a canonical pricing key. Mirrors
/// `coord.usage_rollup.normalize_model`: exact match on a known tier first,
/// then substring match (so a dated model id like `"claude-sonnet-4-6"`
/// still resolves), else `"(unknown)"` — never guessed, never silently
/// priced at $0.
pub(crate) fn normalize_model(model: Option<&str>) -> &'static str {
    let Some(m) = model else { return UNKNOWN_MODEL };
    let text = m.trim().to_lowercase();
    if text.is_empty() || text == UNKNOWN_MODEL {
        return UNKNOWN_MODEL;
    }
    for known in KNOWN_MODELS {
        if text == known {
            return known;
        }
    }
    for known in KNOWN_MODELS {
        if text.contains(known) {
            return known;
        }
    }
    UNKNOWN_MODEL
}

/// Per-1M-token rates for one model tier.
#[derive(Clone, Copy, Debug)]
pub(crate) struct ModelRates {
    pub(crate) input: f64,
    pub(crate) output: f64,
    pub(crate) cache_read: f64,
    pub(crate) cache_creation: f64,
}

/// Hardcoded snapshot of `coord.config.PricingConfig`'s shipped defaults
/// (`coord/config.py`'s `_default_pricing()`) — see the module docs above
/// for why this can't be fetched live from `coordinator.yml`.
pub(crate) fn default_rates(canonical_model: &str) -> Option<ModelRates> {
    match canonical_model {
        "sonnet" => Some(ModelRates { input: 3.00, output: 15.00, cache_read: 0.30, cache_creation: 3.75 }),
        "opus" => Some(ModelRates { input: 15.00, output: 75.00, cache_read: 1.50, cache_creation: 18.75 }),
        "haiku" => Some(ModelRates { input: 1.00, output: 5.00, cache_read: 0.10, cache_creation: 1.25 }),
        _ => None,
    }
}

// ── Per-leg cost / duration / window membership ──────────────────────────────

/// `(cost_captured, cost_est, unknown_model)` for one assignment leg. A leg
/// with a real captured `cost_usd` (non-null, non-zero) keeps it verbatim
/// and never also gets an estimate (no double-counting). A leg with
/// `cost_usd` in `{None, 0}` and any tokens gets an estimate from the
/// hardcoded pricing snapshot, keyed by the leg's normalized model — unless
/// the model doesn't map to a priced tier, in which case `unknown_model` is
/// `true` (never a silent $0). Mirrors `coord.usage_rollup.leg_cost`.
pub(crate) fn leg_cost(a: &Assignment) -> (f64, f64, bool) {
    if let Some(captured) = a.cost_usd {
        if captured != 0.0 {
            return (captured, 0.0, false);
        }
    }
    let total_tokens = a.input_tokens + a.output_tokens + a.cache_read_tokens + a.cache_creation_tokens;
    if total_tokens <= 0 {
        return (0.0, 0.0, false);
    }
    let canonical = normalize_model(a.model.as_deref());
    let Some(rates) = default_rates(canonical) else {
        return (0.0, 0.0, true);
    };
    let est = (a.input_tokens as f64 * rates.input
        + a.output_tokens as f64 * rates.output
        + a.cache_read_tokens as f64 * rates.cache_read
        + a.cache_creation_tokens as f64 * rates.cache_creation)
        / 1_000_000.0;
    (0.0, est, false)
}

/// `(duration_secs, is_open)` for one leg. `is_open` is `true` when there's
/// no `finished_at` (still running): duration contributes 0 and the leg is
/// counted separately. Otherwise `max(0, finished_at - dispatched_at)`.
/// Mirrors `coord.usage_rollup.leg_duration`.
pub(crate) fn leg_duration(a: &Assignment) -> (f64, bool) {
    let Some(finished) = a.finished_at else { return (0.0, true) };
    let Some(dispatched) = a.dispatched_at else { return (0.0, false) };
    ((finished - dispatched).max(0.0), false)
}

/// Whether `a` is in-window: `dispatched_at` OR `finished_at` in `[start,
/// end)`. Mirrors `coord.usage_rollup.leg_in_window`.
pub(crate) fn leg_in_window(a: &Assignment, window: &UsageWindow) -> bool {
    window.contains(a.dispatched_at) || window.contains(a.finished_at)
}

// ── Grouping / aggregation ───────────────────────────────────────────────────

/// Group identity for one `UsageRow`. `issue_number` is `None` for
/// `UsageGroupBy::Repo` rows (the row IS the whole repo). Keying by
/// `(repo, issue_number)` rather than a bare issue number avoids the
/// cross-repo collision `coord.usage_rollup.aggregate`'s `by="issue"` path
/// has (documented there as a known, un-fixed limitation) — GitHub issue
/// numbers are per-repo, not global, and `coordinator.yml` is explicitly
/// multi-repo.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub(crate) struct UsageGroupKey {
    pub(crate) repo: String,
    pub(crate) issue_number: Option<u64>,
}

/// One aggregated row of the Usage grid (or the grand-total row).
#[derive(Clone, Debug, Default)]
pub(crate) struct UsageRow {
    pub(crate) repo: String,
    pub(crate) issue_number: Option<u64>,
    pub(crate) title: String,
    pub(crate) legs: usize,
    pub(crate) cost_captured: f64,
    pub(crate) cost_est: f64,
    pub(crate) tokens_input: i64,
    pub(crate) tokens_output: i64,
    pub(crate) tokens_cache_read: i64,
    pub(crate) tokens_cache_creation: i64,
    pub(crate) duration_secs: f64,
    pub(crate) open_legs: usize,
    pub(crate) unknown_model_legs: usize,
}

impl UsageRow {
    fn for_key(key: &UsageGroupKey) -> Self {
        UsageRow { repo: key.repo.clone(), issue_number: key.issue_number, ..Default::default() }
    }

    /// Captured + estimated cost — never double-counts a leg (see [`leg_cost`]).
    pub(crate) fn cost_total(&self) -> f64 {
        self.cost_captured + self.cost_est
    }

    fn tokens_out_cache(&self) -> i64 {
        self.tokens_output + self.tokens_cache_read
    }

    fn accumulate(&mut self, a: &Assignment) {
        let (captured, est, unknown_model) = leg_cost(a);
        let (duration, is_open) = leg_duration(a);
        self.legs += 1;
        self.cost_captured += captured;
        self.cost_est += est;
        self.tokens_input += a.input_tokens;
        self.tokens_output += a.output_tokens;
        self.tokens_cache_read += a.cache_read_tokens;
        self.tokens_cache_creation += a.cache_creation_tokens;
        self.duration_secs += duration;
        if is_open {
            self.open_legs += 1;
        }
        if unknown_model {
            self.unknown_model_legs += 1;
        }
    }
}

/// Aggregate `assignments` into per-group `UsageRow`s plus a grand total,
/// for legs falling in `window`. Rows are returned sorted desc by
/// [`UsageRow::cost_total`] (the CLI's #1115 default) — callers wanting a
/// different order should re-sort with [`sort_usage_rows`]. Mirrors
/// `coord.usage_rollup.aggregate`/`rollup`.
pub(crate) fn aggregate_usage(
    assignments: &[Assignment],
    window: &UsageWindow,
    group_by: UsageGroupBy,
) -> (Vec<UsageRow>, UsageRow) {
    let mut groups: std::collections::HashMap<UsageGroupKey, UsageRow> = std::collections::HashMap::new();
    let mut totals = UsageRow::default();
    for a in assignments {
        if !leg_in_window(a, window) {
            continue;
        }
        let key = UsageGroupKey {
            repo: a.repo.clone(),
            issue_number: match group_by {
                UsageGroupBy::Issue => Some(a.issue_number),
                UsageGroupBy::Repo => None,
            },
        };
        let row = groups.entry(key.clone()).or_insert_with(|| UsageRow::for_key(&key));
        if group_by == UsageGroupBy::Issue && row.title.is_empty() && !a.issue_title.is_empty() {
            row.title = a.issue_title.clone();
        }
        row.accumulate(a);
        totals.accumulate(a);
    }
    let mut rows: Vec<UsageRow> = groups.into_values().collect();
    sort_usage_rows(&mut rows, UsageSortKey::CostTotal, SortDirection::Descending);
    (rows, totals)
}

/// Legs for one issue, in-window, oldest-first by `dispatched_at` (falling
/// back to `finished_at`) — the per-stage drill-down order. Mirrors
/// `coord.usage.format_usage_issue_drill`'s `_sort_ts`.
pub(crate) fn issue_legs<'a>(
    assignments: &'a [Assignment],
    repo: &str,
    issue_number: u64,
    window: &UsageWindow,
) -> Vec<&'a Assignment> {
    let mut legs: Vec<&Assignment> = assignments
        .iter()
        .filter(|a| a.repo == repo && a.issue_number == issue_number && leg_in_window(a, window))
        .collect();
    legs.sort_by(|a, b| {
        let ts = |x: &Assignment| x.dispatched_at.or(x.finished_at).unwrap_or(f64::INFINITY);
        ts(a).partial_cmp(&ts(b)).unwrap_or(std::cmp::Ordering::Equal)
    });
    legs
}

// ── Sorting ──────────────────────────────────────────────────────────────────

/// Column layout for `UsageGroupBy::Issue`: Issue# / Repo / Title / Legs /
/// Cost / Est / Tokens / Time. Index must match `usage_columns`/
/// `usage_data_rows`.
const ISSUE_SORT_KEYS: [UsageSortKey; 8] = [
    UsageSortKey::IssueNumber,
    UsageSortKey::Repo,
    UsageSortKey::Title,
    UsageSortKey::Legs,
    UsageSortKey::CostCaptured,
    UsageSortKey::CostEst,
    UsageSortKey::Tokens,
    UsageSortKey::Time,
];

/// Column layout for `UsageGroupBy::Repo`: Repo / Legs / Cost / Est /
/// Tokens / Time (no Issue#/Title — the row IS the whole repo).
const REPO_SORT_KEYS: [UsageSortKey; 6] = [
    UsageSortKey::Repo,
    UsageSortKey::Legs,
    UsageSortKey::CostCaptured,
    UsageSortKey::CostEst,
    UsageSortKey::Tokens,
    UsageSortKey::Time,
];

fn sort_keys_for(group_by: UsageGroupBy) -> &'static [UsageSortKey] {
    match group_by {
        UsageGroupBy::Issue => &ISSUE_SORT_KEYS,
        UsageGroupBy::Repo => &REPO_SORT_KEYS,
    }
}

/// The [`UsageSortKey`] a click on grid column `col` selects, or `None` when
/// `col` is out of range for `group_by`'s column layout.
pub(crate) fn column_sort_key(group_by: UsageGroupBy, col: usize) -> Option<UsageSortKey> {
    sort_keys_for(group_by).get(col).copied()
}

/// The column index `key` is shown under for `group_by`, or `None` when
/// `key` has no matching column (`CostTotal` — the default, not tied to any
/// single visible column since Cost/Est are separate columns).
pub(crate) fn column_for_sort_key(group_by: UsageGroupBy, key: UsageSortKey) -> Option<usize> {
    sort_keys_for(group_by).iter().position(|&k| k == key)
}

/// The direction a freshly-clicked (not-previously-active) sort key should
/// start in: identity-ish text columns ascend (A→Z / #1→#N), metrics
/// descend (biggest first — the interesting end).
pub(crate) fn default_sort_direction(key: UsageSortKey) -> SortDirection {
    match key {
        UsageSortKey::IssueNumber | UsageSortKey::Repo | UsageSortKey::Title => {
            SortDirection::Ascending
        }
        _ => SortDirection::Descending,
    }
}

/// Sort `rows` in place by `key`/`dir`. Stable (ties keep their prior
/// relative order — `aggregate_usage`'s default cost-desc order, or
/// whatever the previous sort left them in).
pub(crate) fn sort_usage_rows(rows: &mut [UsageRow], key: UsageSortKey, dir: SortDirection) {
    rows.sort_by(|a, b| {
        let ord = match key {
            UsageSortKey::CostTotal => {
                a.cost_total().partial_cmp(&b.cost_total()).unwrap_or(std::cmp::Ordering::Equal)
            }
            UsageSortKey::IssueNumber => a.issue_number.cmp(&b.issue_number),
            UsageSortKey::Repo => a.repo.cmp(&b.repo),
            UsageSortKey::Title => a.title.cmp(&b.title),
            UsageSortKey::Legs => a.legs.cmp(&b.legs),
            UsageSortKey::CostCaptured => {
                a.cost_captured.partial_cmp(&b.cost_captured).unwrap_or(std::cmp::Ordering::Equal)
            }
            UsageSortKey::CostEst => {
                a.cost_est.partial_cmp(&b.cost_est).unwrap_or(std::cmp::Ordering::Equal)
            }
            UsageSortKey::Tokens => a.tokens_out_cache().cmp(&b.tokens_out_cache()),
            UsageSortKey::Time => {
                a.duration_secs.partial_cmp(&b.duration_secs).unwrap_or(std::cmp::Ordering::Equal)
            }
        };
        match dir {
            SortDirection::Ascending => ord,
            SortDirection::Descending => ord.reverse(),
        }
    });
}

// ── impl CoordApp: render + interaction ──────────────────────────────────────

impl CoordApp {
    /// The current scope resolved against "now".
    pub(crate) fn usage_window(&self) -> UsageWindow {
        self.usage_scope.resolve(unix_now())
    }

    /// Aggregated rows + grand total for the current window/group-by/sort.
    pub(crate) fn usage_rows(&self) -> (Vec<UsageRow>, UsageRow) {
        let window = self.usage_window();
        let (mut rows, totals) = aggregate_usage(&self.data.assignments, &window, self.usage_group_by);
        sort_usage_rows(&mut rows, self.usage_sort_key, self.usage_sort_dir);
        (rows, totals)
    }

    fn usage_selected_idx(&self, rows_len: usize) -> usize {
        if rows_len == 0 {
            0
        } else {
            self.usage_sel.min(rows_len - 1)
        }
    }

    /// Column set for the main grid, depending on the current group-by.
    fn usage_columns(group_by: UsageGroupBy) -> Vec<Column> {
        let mut cols = vec![];
        if group_by == UsageGroupBy::Issue {
            cols.push(Column { title: "Issue".into(), width: ColumnWidth::Fixed(7.0), align: ColumnAlign::Left });
        }
        cols.push(Column {
            title: "Repo".into(),
            width: if group_by == UsageGroupBy::Repo { ColumnWidth::Flex(1.0) } else { ColumnWidth::Fixed(10.0) },
            align: ColumnAlign::Left,
        });
        if group_by == UsageGroupBy::Issue {
            cols.push(Column { title: "Title".into(), width: ColumnWidth::Flex(3.0), align: ColumnAlign::Left });
        }
        cols.push(Column { title: "Legs".into(), width: ColumnWidth::Fixed(5.0), align: ColumnAlign::Right });
        cols.push(Column { title: "Cost".into(), width: ColumnWidth::Fixed(10.0), align: ColumnAlign::Right });
        cols.push(Column { title: "Est (~)".into(), width: ColumnWidth::Fixed(12.0), align: ColumnAlign::Right });
        cols.push(Column { title: "Out/Cache".into(), width: ColumnWidth::Fixed(16.0), align: ColumnAlign::Right });
        cols.push(Column { title: "Time".into(), width: ColumnWidth::Fixed(10.0), align: ColumnAlign::Right });
        cols
    }

    fn usage_cell(text: impl Into<String>) -> StyledText {
        StyledText { spans: vec![StyledSpan::with_fg(text.into(), Color::rgb(200, 200, 200))] }
    }

    fn usage_row_cells(row: &UsageRow, group_by: UsageGroupBy) -> Vec<StyledText> {
        let mut cells = vec![];
        if group_by == UsageGroupBy::Issue {
            let issue_label = row.issue_number.map(|n| format!("#{n}")).unwrap_or_default();
            cells.push(Self::usage_cell(issue_label));
        }
        cells.push(Self::usage_cell(row.repo.clone()));
        if group_by == UsageGroupBy::Issue {
            cells.push(Self::usage_cell(row.title.clone()));
        }
        cells.push(Self::usage_cell(row.legs.to_string()));
        cells.push(Self::usage_cell(format_cost_captured(row.cost_captured)));
        let est_note = if row.unknown_model_legs > 0 { format!(" ⚠{}", row.unknown_model_legs) } else { String::new() };
        cells.push(Self::usage_cell(format!("{}{}", format_cost_est(row.cost_est), est_note)));
        cells.push(Self::usage_cell(format!(
            "{}/{}",
            fmt_tokens(row.tokens_output),
            fmt_tokens(row.tokens_cache_read)
        )));
        let open_note = if row.open_legs > 0 { format!(" (+{} run)", row.open_legs) } else { String::new() };
        cells.push(Self::usage_cell(format!("{}{}", format_duration_usage(row.duration_secs), open_note)));
        cells
    }

    fn usage_data_rows(rows: &[UsageRow], group_by: UsageGroupBy) -> Vec<DataRow> {
        rows.iter()
            .map(|row| DataRow { cells: Self::usage_row_cells(row, group_by), decoration: Decoration::Normal })
            .collect()
    }

    /// Pinned Σ totals row (quadraui #432) for the main grid's footer.
    fn usage_footer_row(totals: &UsageRow, group_by: UsageGroupBy) -> DataRow {
        let mut cells = vec![];
        // "Σ" occupies the leading identity column(s) (Issue+Repo for
        // Issue-grouped, Repo alone for Repo-grouped); the remaining
        // identity column (Title, Issue-grouped only) stays blank.
        cells.push(Self::usage_cell("Σ"));
        if group_by == UsageGroupBy::Issue {
            cells.push(Self::usage_cell(""));
            cells.push(Self::usage_cell(""));
        }
        cells.push(Self::usage_cell(totals.legs.to_string()));
        cells.push(Self::usage_cell(format_cost_captured(totals.cost_captured)));
        let est_note = if totals.unknown_model_legs > 0 {
            format!(" ⚠{}", totals.unknown_model_legs)
        } else {
            String::new()
        };
        cells.push(Self::usage_cell(format!("{}{}", format_cost_est(totals.cost_est), est_note)));
        cells.push(Self::usage_cell(format!(
            "{}/{}",
            fmt_tokens(totals.tokens_output),
            fmt_tokens(totals.tokens_cache_read)
        )));
        let open_note = if totals.open_legs > 0 { format!(" (+{} run)", totals.open_legs) } else { String::new() };
        cells.push(Self::usage_cell(format!("{}{}", format_duration_usage(totals.duration_secs), open_note)));
        DataRow { cells, decoration: Decoration::Header }
    }

    /// Column set for the per-issue drill (stage/model/interactive/cost/
    /// est/tokens/time/status).
    fn usage_drill_columns() -> Vec<Column> {
        vec![
            Column { title: "Stage".into(), width: ColumnWidth::Fixed(11.0), align: ColumnAlign::Left },
            Column { title: "Model".into(), width: ColumnWidth::Fixed(9.0), align: ColumnAlign::Left },
            Column { title: "Int".into(), width: ColumnWidth::Fixed(4.0), align: ColumnAlign::Left },
            Column { title: "Cost".into(), width: ColumnWidth::Fixed(10.0), align: ColumnAlign::Right },
            Column { title: "Est (~)".into(), width: ColumnWidth::Fixed(10.0), align: ColumnAlign::Right },
            Column { title: "In".into(), width: ColumnWidth::Fixed(7.0), align: ColumnAlign::Right },
            Column { title: "Out".into(), width: ColumnWidth::Fixed(7.0), align: ColumnAlign::Right },
            Column { title: "Cache".into(), width: ColumnWidth::Fixed(7.0), align: ColumnAlign::Right },
            Column { title: "Time".into(), width: ColumnWidth::Fixed(10.0), align: ColumnAlign::Right },
            Column { title: "Status".into(), width: ColumnWidth::Flex(1.0), align: ColumnAlign::Left },
        ]
    }

    fn usage_drill_rows(legs: &[&Assignment]) -> Vec<DataRow> {
        legs.iter()
            .map(|a| {
                let (captured, est, unknown_model) = leg_cost(a);
                let (duration, is_open) = leg_duration(a);
                let stage = a.assignment_type.clone().unwrap_or_default();
                let model = a.model.clone().unwrap_or_else(|| UNKNOWN_MODEL.to_string());
                let interactive = if a.is_interactive { "I" } else { "-" };
                let est_str = if unknown_model { "n/a*".to_string() } else { format_cost_est(est) };
                let time_str = if is_open { "running".to_string() } else { format_duration_usage(duration) };
                DataRow {
                    cells: vec![
                        Self::usage_cell(stage),
                        Self::usage_cell(model),
                        Self::usage_cell(interactive),
                        Self::usage_cell(format_cost_captured(captured)),
                        Self::usage_cell(est_str),
                        Self::usage_cell(fmt_tokens(a.input_tokens)),
                        Self::usage_cell(fmt_tokens(a.output_tokens)),
                        Self::usage_cell(fmt_tokens(a.cache_read_tokens)),
                        Self::usage_cell(time_str),
                        Self::usage_cell(a.status.clone()),
                    ],
                    decoration: Decoration::Normal,
                }
            })
            .collect()
    }

    /// Sidebar content: scope / group-by / sort selection, matching the
    /// Audit sidebar's "always show the current filter" convention (#1040
    /// contract §8/§9) so the affordance is discoverable before the
    /// operator touches any of it.
    pub(crate) fn usage_sidebar(&self) -> ListView {
        let (rows, totals) = self.usage_rows();
        let count_line = format!(
            "  {} {}{}",
            rows.len(),
            self.usage_group_by.label().to_lowercase(),
            if rows.len() == 1 { "" } else { "s" }
        );
        let mut items = vec![activity_item(&count_line, Color::rgb(160, 160, 160))];
        items.push(activity_item(
            &format!("  Σ total {}", format_cost_est(totals.cost_total()).trim_start_matches('~')),
            Color::rgb(120, 210, 120),
        ));
        items.push(activity_item(&format!("  Scope: {}", self.usage_scope.label()), Color::rgb(150, 180, 220)));
        items.push(activity_item(&format!("  Group by: {}", self.usage_group_by.label()), Color::rgb(150, 180, 220)));
        ListView {
            id: WidgetId::new("usage-sidebar"),
            title: Some(StyledText::plain(" USAGE ")),
            items,
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: false,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    /// Render the Usage main panel: the per-issue/repo grid, or — while
    /// `usage_expanded` is set — the per-stage drill for that issue.
    pub(crate) fn render_usage_panel(&self, backend: &mut dyn Backend, rect: Rect, lh: f32) {
        let window = self.usage_window();
        if let Some((repo, issue_number)) = self.usage_expanded.clone() {
            self.render_usage_drill(backend, rect, lh, &window, &repo, issue_number);
            return;
        }
        let (rows, totals) = self.usage_rows();
        if rows.is_empty() {
            *self.usage_table_layout.borrow_mut() = None;
            backend.draw_list(rect, &plain_list("usage-empty", "  No usage data in this window.", 0));
            return;
        }
        let sel = self.usage_selected_idx(rows.len());
        let sort_indicator = column_for_sort_key(self.usage_group_by, self.usage_sort_key)
            .map(|col| (col, self.usage_sort_dir));
        let table = DataTable {
            id: WidgetId::new("usage-grid"),
            columns: Self::usage_columns(self.usage_group_by),
            rows: Self::usage_data_rows(&rows, self.usage_group_by),
            selected_idx: Some(sel),
            scroll_offset: self.usage_scroll,
            sort: sort_indicator,
            has_focus: true,
            show_scrollbar: true,
            min_total_width: None,
            h_scroll: 0.0,
            column_overrides: Vec::new(),
            footer: Some(Self::usage_footer_row(&totals, self.usage_group_by)),
        };
        let layout = backend.draw_data_table(rect, &table, None);
        *self.usage_table_layout.borrow_mut() = Some(layout);
    }

    fn render_usage_drill(
        &self,
        backend: &mut dyn Backend,
        rect: Rect,
        lh: f32,
        window: &UsageWindow,
        repo: &str,
        issue_number: u64,
    ) {
        let legs = issue_legs(&self.data.assignments, repo, issue_number, window);
        let mut total_captured = 0.0;
        let mut total_est = 0.0;
        for a in &legs {
            let (captured, est, _) = leg_cost(a);
            total_captured += captured;
            total_est += est;
        }
        let title = legs.first().map(|a| a.issue_title.clone()).unwrap_or_default();
        let header = format!(
            "  #{issue_number}  {repo}  {title}   {} captured  +  {} est   (Esc = back to grid)",
            format_cost_captured(total_captured),
            format_cost_est(total_est)
        );
        let header_h = lh.max(1.0);
        let header_rect = Rect::new(rect.x, rect.y, rect.width, header_h);
        let table_rect = Rect::new(rect.x, rect.y + header_h, rect.width, (rect.height - header_h).max(0.0));
        backend.draw_list(header_rect, &plain_list("usage-drill-header", &header, 0));
        if legs.is_empty() {
            *self.usage_table_layout.borrow_mut() = None;
            backend.draw_list(table_rect, &plain_list("usage-drill-empty", "  No legs in this window.", 0));
            return;
        }
        let sel = self.usage_selected_idx(legs.len());
        let table = DataTable {
            id: WidgetId::new("usage-drill"),
            columns: Self::usage_drill_columns(),
            rows: Self::usage_drill_rows(&legs),
            selected_idx: Some(sel),
            scroll_offset: self.usage_scroll,
            sort: None,
            has_focus: true,
            show_scrollbar: true,
            min_total_width: None,
            h_scroll: 0.0,
            column_overrides: Vec::new(),
            footer: None,
        };
        let layout = backend.draw_data_table(table_rect, &table, None);
        *self.usage_table_layout.borrow_mut() = Some(layout);
    }

    /// Hit-test a click position against the last-rendered Usage
    /// `DataTable` (grid or drill) layout, same render-then-hit-test
    /// pattern as `audit_table_hit`.
    pub(crate) fn usage_table_hit(&self, pos: Point, main_b: Rect) -> Option<DataTableHit> {
        let layout_ref = self.usage_table_layout.borrow();
        let layout = layout_ref.as_ref()?;
        let x = pos.x - main_b.x;
        let y = pos.y - main_b.y;
        let total_rows = if self.usage_expanded.is_some() {
            let window = self.usage_window();
            let (repo, issue_number) = self.usage_expanded.clone()?;
            issue_legs(&self.data.assignments, &repo, issue_number, &window).len()
        } else {
            self.usage_rows().0.len()
        };
        Some(layout.hit_test(x, y, self.usage_scroll, total_rows))
    }

    /// Click a column header on the main grid: toggle direction on a repeat
    /// click of the already-active key, else switch to the clicked
    /// column's key at its default direction. No-op while drilled in (the
    /// drill table has no sort).
    pub(crate) fn usage_sort_by_column(&mut self, col: usize) {
        let Some(key) = column_sort_key(self.usage_group_by, col) else { return };
        if self.usage_sort_key == key {
            self.usage_sort_dir = match self.usage_sort_dir {
                SortDirection::Ascending => SortDirection::Descending,
                SortDirection::Descending => SortDirection::Ascending,
            };
        } else {
            self.usage_sort_key = key;
            self.usage_sort_dir = default_sort_direction(key);
        }
        self.usage_sel = 0;
        self.usage_scroll = 0;
    }

    /// Expand the selected grid row into its per-stage drill (Issue
    /// group-by only — a no-op for Repo-grouped rows, see module docs).
    pub(crate) fn usage_try_expand_selected(&mut self) {
        if self.usage_group_by != UsageGroupBy::Issue {
            return;
        }
        let (rows, _totals) = self.usage_rows();
        if let Some(row) = rows.get(self.usage_selected_idx(rows.len())) {
            if let Some(issue_number) = row.issue_number {
                self.usage_expanded = Some((row.repo.clone(), issue_number));
                self.usage_sel = 0;
                self.usage_scroll = 0;
            }
        }
    }

    /// Collapse the drill back to the grid.
    pub(crate) fn usage_collapse(&mut self) {
        self.usage_expanded = None;
        self.usage_sel = 0;
        self.usage_scroll = 0;
    }

    /// Keep `usage_sel` inside the visible window — same structural pattern
    /// as `fix_audit_scroll`.
    pub(crate) fn fix_usage_scroll(&mut self, visible: usize) {
        if visible == 0 {
            return;
        }
        if self.usage_sel < self.usage_scroll {
            self.usage_scroll = self.usage_sel;
        } else if self.usage_sel >= self.usage_scroll + visible {
            self.usage_scroll = self.usage_sel + 1 - visible;
        }
    }

    /// Row count for the currently-visible table (grid or drill) — used by
    /// keyboard nav to clamp `usage_sel`.
    pub(crate) fn usage_visible_row_count(&self) -> usize {
        if let Some((repo, issue_number)) = self.usage_expanded.clone() {
            let window = self.usage_window();
            issue_legs(&self.data.assignments, &repo, issue_number, &window).len()
        } else {
            self.usage_rows().0.len()
        }
    }

    /// Open step 1 of the "Custom range…" dialog (`c`).
    pub(crate) fn open_usage_custom_range(&mut self) {
        self.pending_usage_range_start = Some(PendingUsageRangeStart::default());
    }

    /// Step 1 Enter: parse the start instant. On success, move to step 2;
    /// on failure, close the dialog and toast (this codebase's universal
    /// dialog convention — see `PendingUsageRangeStart`'s docs).
    pub(crate) fn submit_usage_range_start(&mut self) {
        let Some(input) = self.pending_usage_range_start.take() else { return };
        match parse_datetime_utc(&input.buf) {
            Some(start) => {
                self.pending_usage_range_end = Some(PendingUsageRangeEnd { start, buf: String::new() });
            }
            None => {
                self.push_toast(
                    "Custom range",
                    "Couldn't parse the start date — expected YYYY-MM-DD or YYYY-MM-DD HH:MM (UTC).",
                    ToastSeverity::Warning,
                );
            }
        }
    }

    /// Step 2 Enter: parse the end instant and, when it resolves to a
    /// non-empty interval, apply `usage_scope = Custom { start, end }`.
    pub(crate) fn submit_usage_range_end(&mut self) {
        let Some(input) = self.pending_usage_range_end.take() else { return };
        match parse_datetime_utc(&input.buf) {
            Some(end) if end > input.start => {
                self.usage_scope = UsageScope::Custom { start: input.start, end };
                self.usage_sel = 0;
                self.usage_scroll = 0;
                self.usage_expanded = None;
            }
            Some(_) => {
                self.push_toast("Custom range", "End must be after start.", ToastSeverity::Warning);
            }
            None => {
                self.push_toast(
                    "Custom range",
                    "Couldn't parse the end date — expected YYYY-MM-DD or YYYY-MM-DD HH:MM (UTC).",
                    ToastSeverity::Warning,
                );
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── civil-calendar round-trip (Howard Hinnant's algorithm) ──────────────

    #[test]
    fn civil_days_round_trip_known_dates() {
        let cases: [(i64, u32, u32, i64); 8] = [
            (1970, 1, 1, 0),
            (2026, 7, 18, 20_652),
            (2000, 3, 1, 11_017),
            (1999, 12, 31, 10_956),
            (2024, 2, 29, 19_782),
            (1600, 2, 29, -135_081),
            (2026, 1, 1, 20_454),
            (2026, 12, 31, 20_818),
        ];
        for (y, m, d, expected_days) in cases {
            let days = days_from_civil(y, m, d);
            assert_eq!(days, expected_days, "days_from_civil({y}, {m}, {d})");
            assert_eq!(civil_from_days(days), (y, m, d), "civil_from_days({days}) round-trip");
        }
    }

    #[test]
    fn utc_month_bounds_matches_civil_dates() {
        // 2026-07-18 12:00 UTC -> July 1 00:00 .. Aug 1 00:00.
        let now = days_from_civil(2026, 7, 18) as f64 * SECS_PER_DAY + 12.0 * 3600.0;
        let (start, end) = utc_month_bounds(now);
        assert_eq!(start, days_from_civil(2026, 7, 1) as f64 * SECS_PER_DAY);
        assert_eq!(end, days_from_civil(2026, 8, 1) as f64 * SECS_PER_DAY);
    }

    #[test]
    fn utc_month_bounds_handles_december_rollover() {
        let now = days_from_civil(2026, 12, 15) as f64 * SECS_PER_DAY;
        let (start, end) = utc_month_bounds(now);
        assert_eq!(start, days_from_civil(2026, 12, 1) as f64 * SECS_PER_DAY);
        assert_eq!(end, days_from_civil(2027, 1, 1) as f64 * SECS_PER_DAY);
    }

    #[test]
    fn utc_week_start_is_monday_for_a_known_thursday() {
        // Epoch day 0 (1970-01-01) is a Thursday; Monday of that week is
        // 1969-12-29, i.e. epoch day -3.
        let start = utc_week_start(0.0);
        assert_eq!(start, -3.0 * SECS_PER_DAY);
    }

    #[test]
    fn utc_day_start_truncates_to_midnight() {
        let now = 10.0 * SECS_PER_DAY + 12345.0;
        assert_eq!(utc_day_start(now), 10.0 * SECS_PER_DAY);
    }

    // ── datetime parsing ─────────────────────────────────────────────────────

    #[test]
    fn parse_datetime_date_only_is_midnight_utc() {
        let ts = parse_datetime_utc("2026-07-18").unwrap();
        assert_eq!(ts, days_from_civil(2026, 7, 18) as f64 * SECS_PER_DAY);
    }

    #[test]
    fn parse_datetime_with_time_adds_hhmm() {
        let ts = parse_datetime_utc("2026-07-18 09:30").unwrap();
        assert_eq!(ts, days_from_civil(2026, 7, 18) as f64 * SECS_PER_DAY + 9.0 * 3600.0 + 30.0 * 60.0);
    }

    #[test]
    fn parse_datetime_accepts_t_separator() {
        let a = parse_datetime_utc("2026-07-18T09:30").unwrap();
        let b = parse_datetime_utc("2026-07-18 09:30").unwrap();
        assert_eq!(a, b);
    }

    #[test]
    fn parse_datetime_rejects_garbage() {
        assert_eq!(parse_datetime_utc("not a date"), None);
        assert_eq!(parse_datetime_utc("2026-13-01"), None);
        assert_eq!(parse_datetime_utc("2026-07-32"), None);
        assert_eq!(parse_datetime_utc("2026-07-18 25:00"), None);
        assert_eq!(parse_datetime_utc(""), None);
    }

    #[test]
    fn format_civil_datetime_round_trips_parse() {
        let ts = parse_datetime_utc("2026-07-18 09:30").unwrap();
        assert_eq!(format_civil_datetime(ts), "2026-07-18 09:30");
    }

    // ── model normalization ──────────────────────────────────────────────────

    #[test]
    fn normalize_model_exact_and_alias() {
        assert_eq!(normalize_model(Some("sonnet")), "sonnet");
        assert_eq!(normalize_model(Some("claude-sonnet-4-6")), "sonnet");
        assert_eq!(normalize_model(Some("claude-opus-4-7")), "opus");
        assert_eq!(normalize_model(Some("claude-haiku-4-5")), "haiku");
    }

    #[test]
    fn normalize_model_unknown_and_empty() {
        assert_eq!(normalize_model(None), UNKNOWN_MODEL);
        assert_eq!(normalize_model(Some("")), UNKNOWN_MODEL);
        assert_eq!(normalize_model(Some("gpt-4")), UNKNOWN_MODEL);
    }

    // ── leg cost / duration / window ─────────────────────────────────────────

    fn make_leg(cost_usd: Option<f64>, model: Option<&str>, input: i64, output: i64, cache_read: i64) -> Assignment {
        let mut a = crate::app::fixtures::make_assignment_typed("done", 1, "repo", Some("work"));
        a.cost_usd = cost_usd;
        a.model = model.map(str::to_string);
        a.input_tokens = input;
        a.output_tokens = output;
        a.cache_read_tokens = cache_read;
        a
    }

    #[test]
    fn leg_cost_captured_wins_no_estimate() {
        let a = make_leg(Some(0.50), Some("sonnet"), 10_000, 100_000, 1_000_000);
        assert_eq!(leg_cost(&a), (0.50, 0.0, false));
    }

    #[test]
    fn leg_cost_estimates_from_tokens_when_uncaptured() {
        // Matches the ms-37 contract fixture's L2 leg exactly:
        // 2k*3 + 50k*15 + 500k*0.30 (per-1M) = 0.006+0.750+0.150 = $0.9060
        let a = make_leg(None, Some("sonnet"), 2_000, 50_000, 500_000);
        let (captured, est, unknown) = leg_cost(&a);
        assert_eq!(captured, 0.0);
        assert!((est - 0.9060).abs() < 1e-9);
        assert!(!unknown);
    }

    #[test]
    fn leg_cost_unknown_model_flagged_not_zero_silently() {
        let a = make_leg(None, Some("gpt-4"), 1_000, 30_000, 300_000);
        let (captured, est, unknown) = leg_cost(&a);
        assert_eq!((captured, est), (0.0, 0.0));
        assert!(unknown);
    }

    #[test]
    fn leg_cost_no_tokens_no_captured_is_zero() {
        let a = make_leg(None, Some("sonnet"), 0, 0, 0);
        assert_eq!(leg_cost(&a), (0.0, 0.0, false));
    }

    #[test]
    fn leg_duration_open_when_no_finished_at() {
        let mut a = make_leg(None, Some("sonnet"), 0, 0, 0);
        a.dispatched_at = Some(100.0);
        a.finished_at = None;
        assert_eq!(leg_duration(&a), (0.0, true));
    }

    #[test]
    fn leg_duration_clamped_nonnegative() {
        let mut a = make_leg(None, Some("sonnet"), 0, 0, 0);
        a.dispatched_at = Some(500.0);
        a.finished_at = Some(400.0); // clock skew
        assert_eq!(leg_duration(&a), (0.0, false));
    }

    #[test]
    fn window_membership_dispatched_or_finished() {
        let window = UsageWindow { start: Some(100.0), end: Some(200.0) };
        let mut a = make_leg(None, Some("sonnet"), 0, 0, 0);
        a.dispatched_at = Some(50.0);
        a.finished_at = Some(150.0);
        assert!(leg_in_window(&a, &window), "finished inside window");
        a.finished_at = Some(300.0);
        a.dispatched_at = Some(150.0);
        assert!(leg_in_window(&a, &window), "dispatched inside window");
        a.dispatched_at = Some(0.0);
        a.finished_at = Some(400.0);
        assert!(!leg_in_window(&a, &window), "spans window but touches neither end");
    }

    #[test]
    fn window_boundary_is_half_open() {
        let window = UsageWindow { start: Some(100.0), end: Some(200.0) };
        assert!(window.contains(Some(100.0)));
        assert!(!window.contains(Some(200.0)));
        assert!(window.contains(Some(199.999)));
    }

    // ── aggregation ───────────────────────────────────────────────────────────

    #[test]
    fn aggregate_by_issue_sums_and_sorts_desc_by_total_cost() {
        let mut a1 = make_leg(Some(0.50), Some("sonnet"), 0, 0, 0);
        a1.repo = "alpha".into();
        a1.issue_number = 501;
        a1.issue_title = "Alpha issue".into();
        a1.dispatched_at = Some(1_000.0);
        a1.finished_at = Some(1_600.0);

        let mut a2 = make_leg(Some(2.00), Some("opus"), 0, 0, 0);
        a2.repo = "beta".into();
        a2.issue_number = 502;
        a2.issue_title = "Beta issue".into();
        a2.dispatched_at = Some(1_000.0);
        a2.finished_at = Some(2_200.0);

        let window = UsageWindow { start: Some(0.0), end: Some(10_000.0) };
        let (rows, totals) = aggregate_usage(&[a1, a2], &window, UsageGroupBy::Issue);
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].issue_number, Some(502), "higher cost sorts first");
        assert_eq!(rows[1].issue_number, Some(501));
        assert_eq!(totals.legs, 2);
        assert!((totals.cost_captured - 2.50).abs() < 1e-9);
    }

    #[test]
    fn aggregate_by_repo_collapses_issues_within_a_repo() {
        let mut a1 = make_leg(Some(1.0), Some("sonnet"), 0, 0, 0);
        a1.repo = "alpha".into();
        a1.issue_number = 1;
        let mut a2 = make_leg(Some(1.0), Some("sonnet"), 0, 0, 0);
        a2.repo = "alpha".into();
        a2.issue_number = 2;

        let window = UsageWindow { start: None, end: None };
        let (rows, _totals) = aggregate_usage(&[a1, a2], &window, UsageGroupBy::Repo);
        assert_eq!(rows.len(), 1, "same-repo different-issue rows collapse into one Repo row");
        assert_eq!(rows[0].legs, 2);
        assert_eq!(rows[0].issue_number, None);
    }

    #[test]
    fn aggregate_excludes_out_of_window_legs_from_totals() {
        let mut a = make_leg(Some(5.0), Some("sonnet"), 0, 0, 0);
        a.dispatched_at = Some(50.0);
        a.finished_at = Some(60.0);
        let window = UsageWindow { start: Some(100.0), end: Some(200.0) };
        let (rows, totals) = aggregate_usage(&[a], &window, UsageGroupBy::Issue);
        assert!(rows.is_empty());
        assert_eq!(totals.legs, 0);
        assert_eq!(totals.cost_captured, 0.0);
    }

    #[test]
    fn aggregate_by_issue_keys_on_repo_and_number_no_cross_repo_collision() {
        let mut a1 = make_leg(Some(1.0), Some("sonnet"), 0, 0, 0);
        a1.repo = "alpha".into();
        a1.issue_number = 501;
        let mut a2 = make_leg(Some(1.0), Some("sonnet"), 0, 0, 0);
        a2.repo = "beta".into();
        a2.issue_number = 501; // same number, different repo

        let window = UsageWindow { start: None, end: None };
        let (rows, _totals) = aggregate_usage(&[a1, a2], &window, UsageGroupBy::Issue);
        assert_eq!(rows.len(), 2, "same issue # in different repos must not collapse into one row");
    }

    #[test]
    fn issue_legs_sorted_oldest_first() {
        let mut a1 = make_leg(Some(1.0), Some("sonnet"), 0, 0, 0);
        a1.repo = "alpha".into();
        a1.issue_number = 1;
        a1.assignment_type = Some("review".into());
        a1.dispatched_at = Some(2_000.0);

        let mut a2 = make_leg(Some(1.0), Some("sonnet"), 0, 0, 0);
        a2.repo = "alpha".into();
        a2.issue_number = 1;
        a2.assignment_type = Some("work".into());
        a2.dispatched_at = Some(1_000.0);

        let window = UsageWindow { start: None, end: None };
        let assignments = [a1, a2];
        let legs = issue_legs(&assignments, "alpha", 1, &window);
        assert_eq!(legs.len(), 2);
        assert_eq!(legs[0].assignment_type.as_deref(), Some("work"), "oldest dispatched_at first");
        assert_eq!(legs[1].assignment_type.as_deref(), Some("review"));
    }

    // ── sorting ───────────────────────────────────────────────────────────────

    #[test]
    fn sort_usage_rows_by_legs_ascending() {
        let mut rows = vec![
            UsageRow { legs: 3, ..Default::default() },
            UsageRow { legs: 1, ..Default::default() },
            UsageRow { legs: 2, ..Default::default() },
        ];
        sort_usage_rows(&mut rows, UsageSortKey::Legs, SortDirection::Ascending);
        assert_eq!(rows.iter().map(|r| r.legs).collect::<Vec<_>>(), vec![1, 2, 3]);
    }

    #[test]
    fn column_sort_key_maps_issue_columns() {
        assert_eq!(column_sort_key(UsageGroupBy::Issue, 0), Some(UsageSortKey::IssueNumber));
        assert_eq!(column_sort_key(UsageGroupBy::Issue, 1), Some(UsageSortKey::Repo));
        assert_eq!(column_sort_key(UsageGroupBy::Issue, 2), Some(UsageSortKey::Title));
        assert_eq!(column_sort_key(UsageGroupBy::Issue, 100), None);
    }

    #[test]
    fn column_sort_key_maps_repo_columns_no_title() {
        assert_eq!(column_sort_key(UsageGroupBy::Repo, 0), Some(UsageSortKey::Repo));
        assert_eq!(column_sort_key(UsageGroupBy::Repo, 1), Some(UsageSortKey::Legs));
    }

    #[test]
    fn column_for_sort_key_cost_total_has_no_column() {
        assert_eq!(column_for_sort_key(UsageGroupBy::Issue, UsageSortKey::CostTotal), None);
        assert_eq!(column_for_sort_key(UsageGroupBy::Issue, UsageSortKey::Legs), Some(3));
    }

    #[test]
    fn usage_scope_cycle_and_resolve() {
        assert_eq!(UsageScope::Today.cycle_next(), UsageScope::Week);
        assert_eq!(UsageScope::Week.cycle_next(), UsageScope::Month);
        assert_eq!(UsageScope::Month.cycle_next(), UsageScope::Today);
        assert_eq!(UsageScope::Custom { start: 0.0, end: 1.0 }.cycle_next(), UsageScope::Today);

        let now = days_from_civil(2026, 7, 18) as f64 * SECS_PER_DAY + 3600.0;
        let window = UsageScope::Today.resolve(now);
        assert_eq!(window.start, Some(days_from_civil(2026, 7, 18) as f64 * SECS_PER_DAY));
        assert_eq!(window.end, Some(days_from_civil(2026, 7, 19) as f64 * SECS_PER_DAY));
    }

    // ── cost formatting — 4 decimal places (ms-37 contract Mock 2) ──────────

    #[test]
    fn format_cost_captured_renders_four_decimal_places() {
        // ms-37 contract Mock 2: L1 captured cost $0.5000.
        assert_eq!(format_cost_captured(0.50), "$0.5000");
        // Whole-dollar captured costs also get 4 dp.
        assert_eq!(format_cost_captured(2.00), "$2.0000");
        assert_eq!(format_cost_captured(2.5), "$2.5000");
        // Zero / negative → em dash (no captured cost).
        assert_eq!(format_cost_captured(0.0), "—");
        assert_eq!(format_cost_captured(-1.0), "—");
    }

    #[test]
    fn format_cost_est_renders_four_decimal_places() {
        // ms-37 contract Mock 2: L2 est ~$0.9060 (NOT ~$0.91).
        assert_eq!(format_cost_est(0.9060), "~$0.9060");
        // L4 est ~$1.4520.
        assert_eq!(format_cost_est(1.4520), "~$1.4520");
        // Zero → em dash (no estimate).
        assert_eq!(format_cost_est(0.0), "—");
        // Sub-0.0001 (pathologically small) → ~< $0.0001.
        assert_eq!(format_cost_est(0.00005), "~< $0.0001");
    }

    // ── TuiDriver black-box: Usage grid renders 4-decimal cost strings ────────

    /// Drive the full render pipeline via `TuiDriver`/`TestBackend` and confirm
    /// the per-issue grid contains the 4-decimal captured and estimated cost
    /// strings from the ms-37 contract fixture (#501 / alpha).
    ///
    /// Uses a `UsageScope::Custom` window that brackets the fixture timestamps
    /// so the test is not sensitive to the wall-clock date.
    #[test]
    fn usage_grid_renders_four_decimal_cost_strings() {
        use quadraui::tui::testing::driver_with_shell;

        // Build ms-37 fixture legs (contract § "seeded board"):
        //   L1: #501 alpha  work  sonnet  cost_usd=0.50  → captured $0.5000
        //   L2: #501 alpha  review sonnet cost_usd=null  2k/50k/500k → est ~$0.9060
        let base_ts = 1_000_000.0_f64; // arbitrary epoch inside our custom window

        let mut l1 = crate::app::fixtures::make_assignment_typed("done", 501, "alpha", Some("work"));
        l1.issue_title = "Alpha feature".into();
        l1.cost_usd = Some(0.50);
        l1.model = Some("sonnet".into());
        l1.input_tokens = 10_000;
        l1.output_tokens = 100_000;
        l1.cache_read_tokens = 1_000_000;
        l1.dispatched_at = Some(base_ts);
        l1.finished_at = Some(base_ts + 600.0);

        let mut l2 = crate::app::fixtures::make_assignment_typed("done", 501, "alpha", Some("review"));
        l2.issue_title = "Alpha feature".into();
        l2.cost_usd = None;
        l2.model = Some("sonnet".into());
        l2.is_interactive = true;
        l2.input_tokens = 2_000;
        l2.output_tokens = 50_000;
        l2.cache_read_tokens = 500_000;
        l2.dispatched_at = Some(base_ts + 700.0);
        l2.finished_at = Some(base_ts + 1_000.0);

        let mut app = crate::app::fixtures::make_app_with_assignments(vec![l1, l2]);

        // Use a custom window that covers our fixture timestamps — avoids
        // sensitivity to the wall-clock date (Today would miss them).
        app.usage_scope = UsageScope::Custom {
            start: base_ts - 1.0,
            end: base_ts + 10_000.0,
        };
        app.usage_group_by = UsageGroupBy::Issue;

        let mut driver = driver_with_shell(app, CoordApp::shell_config(), 160, 40);

        // Navigate to the Usage panel via its activity-bar icon "$".
        let (x, y) = driver.find("$").unwrap_or_else(|| {
            panic!("Usage activity-bar icon '$' not found:\n{}", driver.screen())
        });
        driver.click(x, y);

        let screen = driver.screen();

        // The grid must contain 4-decimal captured cost for L1.
        assert!(
            screen.contains("$0.5000"),
            "grid must render captured cost $0.5000 (4 dp):\n{screen}"
        );

        // The grid must contain 4-decimal estimated cost for L2
        // (2k*3 + 50k*15 + 500k*0.30 per-1M = $0.9060).
        assert!(
            screen.contains("~$0.9060"),
            "grid must render estimated cost ~$0.9060 (4 dp):\n{screen}"
        );

        // Sanity: the issue number must be visible.
        assert!(screen.contains("#501"), "grid must show issue #501:\n{screen}");
    }
}
