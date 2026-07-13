# ms-37 — Spend & Time Observability — Gate-A contract

Black-box surface for milestone #37 (epic #1117). The **mocks below are the assertion fixtures**
(cli-pytest `*.out` = exact expected `coord usage` stdout; TUI `.screen` for #1116). The suite seeds
**both** the board rows **and** the pricing table, so expected numbers are self-contained and do not
drift with real Anthropic prices. The shipped `pricing:` config defaults are verified separately at
review — the acceptance suite never depends on them.

## Fixture — pricing table (per 1M tokens; FIXTURE values, not shipped defaults)

| model | input | output | cache_read | cache_creation |
|---|---|---|---|---|
| sonnet | 3.00 | 15.00 | 0.30 | 3.75 |
| opus | 15.00 | 75.00 | 1.50 | 18.75 |
| (unknown) | — | — | — | — |

## Fixture — seeded board (6 legs, 2 issues, 2 repos, window = "today")

| leg | issue | repo | type | model | int | cost_usd | in | out | cache_read | dur |
|---|---|---|---|---|---|---|---|---|---|---|
| L1 | 501 | alpha | work | sonnet | — | 0.50 | 10k | 100k | 1,000k | 600s |
| L2 | 501 | alpha | review | sonnet | I | null | 2k | 50k | 500k | 300s |
| L3 | 502 | beta | work | opus | — | 2.00 | 20k | 200k | 2,000k | 1200s |
| L4 | 502 | beta | smoke | sonnet | I | null | 4k | 80k | 800k | 400s |
| L5 | 502 | beta | chat | (unknown) | I | null | 1k | 30k | 300k | 200s |
| L6 | 502 | beta | work | sonnet | — | null | 0 | 0 | 0 | running (no finished_at) |

## Semantics the suite pins

- **Estimate** fires only for legs with `cost_usd ∈ {null,0}` AND tokens>0: `est = Σ tokensₖ·rateₖ(model)`.
  - L2 (sonnet): 2k·3 + 50k·15 + 500k·0.30 (per-1M) = 0.006+0.750+0.150 = **$0.9060**
  - L4 (sonnet): 4k·3 + 80k·15 + 800k·0.30 = 0.012+1.200+0.240 = **$1.4520**
  - L5 (unknown model): **no estimate**, group flagged `unknown-model:1` (never silently $0).
- **Captured** cost is kept as-is, never double-counted (L1 $0.50, L3 $2.00).
- **Duration** = Σ (finished−dispatched); L6 running → contributes 0, counted as `1 in progress`.
- **Window** is a resolved half-open **`[start, end)` interval**. Presets (`today`/`week`/`month`) and
  `--since <ISO|Nd|Nh>` are *specializations* that compute into it; the general case is an **explicit
  start+end** (the TUI #1116 exposes an arbitrary range picker; the CLI may expose `--since/--until`
  later — out of scope now). A leg is in-window if `dispatched_at` OR `finished_at` ∈ `[start, end)`.
  Local time. The header prints the resolved range — e.g. a preset renders `window: today`, a custom
  range renders `window: 2026-07-01 00:00 → 2026-07-08 00:00`.
- **Sort:** default desc by `total` (captured+est). `--sort tokens|time` reorder.

## Mock 1 — `coord usage --today --by-issue` → `by_issue.out`

```
USAGE — by issue — window: today
 issue   repo    legs   cost       est(~)      out / cache      time      note
 #502    beta      4    $2.0000    ~$1.4520    310k / 3.1M      30m00s    ⚠ unknown-model:1
 #501    alpha     2    $0.5000    ~$0.9060    150k / 1.5M      15m00s
 ────────────────────────────────────────────────────────────────────────────────
 Σ  captured $2.5000 · est ~$2.3580 · total $4.8580 · 460k out / 4.6M cache · 45m00s · 1 in progress
```

## Mock 2 — `coord usage --issue 502` (per-stage drill) → `issue_502.out`

```
#502  beta   $2.0000 captured  +  ~$1.4520 est
 stage    model      int   cost       est(~)      out    cache    time      status
 work     opus       -     $2.0000     —          200k   2.0M     20m00s    merged
 smoke    sonnet     I      —         ~$1.4520     80k   0.8M      6m40s    done
 chat     (unknown)  I      —          n/a*        30k   0.3M      3m20s    done    *unknown model
 work     sonnet     -      —           —            0     0        —        running
```

## Mock 3 — `coord usage --by repo` (cross-repo; CLI-2 slice) → `by_repo.out`

```
USAGE — by repo — window: today
 repo    issues  legs   cost       est(~)      total       out / cache     time
 beta         1     4    $2.0000    ~$1.4520    $3.4520     310k / 3.1M     30m00s
 alpha        1     2    $0.5000    ~$0.9060    $1.4060     150k / 1.5M     15m00s
 ──────────────────────────────────────────────────────────────────────────────
 Σ  total $4.8580 · 460k out / 4.6M cache · 45m00s · 1 in progress
```

## Mock 4 — `coord usage --by-time` (where time goes; CLI-2 slice) → `by_time.out`

```
USAGE — time by stage — window: today
 stage         legs   time      share
 work             3    30m00s    66.7%   (1 in progress)
 smoke            1     6m40s    14.8%
 review           1     5m00s    11.1%
 chat             1     3m20s     7.4%
 ── total active 45m00s ──
 (also available: --by-time --by issue → per-issue duration ranking)
```

## Manifest (test-id → issue slice)

- `test_by_issue_*`, `test_issue_drill_*`, `test_estimator_*`, `test_window_*` → **#1118 Core / #1115 CLI-1**
- `test_by_repo_*`, `test_by_time_*`, `test_since_week_month_*` → **#1119 CLI-2**
- `.screen` grid mocks (same fixture, TUI render) → **#1116 TUI**
- #1125 (drivers) is validated by its own pytest unit tests (normal Test gate), not a sealed slice.

> Column layout/rounding above is the reviewable surface — the mock the human signs off on. Workers
> implement to reproduce these exact strings for the seeded fixture; they may not edit the suite.
