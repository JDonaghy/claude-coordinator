"""ms-37 / issue #1118 — Usage Core: pure cost/token/duration aggregation.

These tests pin the **numeric black-box surface** of the pure aggregator
``coord.usage_rollup`` — the rollup that sits *behind* the rendered
``coord usage`` tables (Mock 1 "by issue" and Mock 2 "issue drill" in
``contract.md``). #1118 ships NO CLI rendering (that is #1115 CLI-1 / #1119
CLI-2), so the assertions target the pure data structure, not stdout strings.

API surface exercised (derived from the issue's stated input/output fields;
function/structure names chosen by the test-author where the contract only
named the *fields* — see the ms-37 authoring summary):

    from coord.usage_rollup import (
        aggregate,          # (rows, *, by, window, pricing) -> rollup dict
        estimate_leg_cost,  # (row, pricing) -> float | None  (None = unmapped model)
        canonical_model,    # (name) -> canonical key (alias normalization)
        Window,             # half-open [start, end) interval
        in_window,          # (row, window) -> bool
    )

``aggregate`` returns a dict shaped as::

    {
      "by": "issue",
      "groups": [  # sorted desc by cost_total by default
        {
          "key": 502, "legs": 4,
          "cost_captured": 2.0, "cost_est": 1.452, "cost_total": 3.452,
          "tokens": {"input": .., "output": .., "cache_read": .., "cache_creation": ..},
          "duration_secs": 1800.0, "open_legs": 1, "unknown_models": 1,
          "rows": [ <the retained per-leg dicts, in input order> ],
        }, ...
      ],
      "totals": { <same numeric keys, across all in-window legs> },
    }

Every expected number below comes straight from ``contract.md`` §"Semantics
the suite pins" and the Mock tables. The fixtures live in ``conftest.py``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.filterwarnings("ignore")

# Estimator ground truth from the contract:
#   L2 (sonnet): 2k*3 + 50k*15 + 500k*0.30  (per-1M) = 0.906
#   L4 (sonnet): 4k*3 + 80k*15 + 800k*0.30            = 1.452
EST_L2 = 0.9060
EST_L4 = 1.4520


def _group(rollup, key):
    """The single group whose ``key`` == *key* (fails loudly if absent)."""
    matches = [g for g in rollup["groups"] if g["key"] == key]
    assert matches, f"no group with key={key!r} in {[g['key'] for g in rollup['groups']]}"
    assert len(matches) == 1, f"duplicate groups for key={key!r}"
    return matches[0]


# ---------------------------------------------------------------------------
# by-issue rollup — the numbers behind Mock 1
# ---------------------------------------------------------------------------

def test_by_issue_leg_counts(board_rows, pricing, today_window):
    rollup = aggregate_(board_rows, by="issue", window=today_window, pricing=pricing)
    assert _group(rollup, 502)["legs"] == 4      # L3,L4,L5,L6
    assert _group(rollup, 501)["legs"] == 2      # L1,L2


def test_by_issue_captured_cost(board_rows, pricing, today_window):
    rollup = aggregate_(board_rows, by="issue", window=today_window, pricing=pricing)
    assert _group(rollup, 502)["cost_captured"] == pytest.approx(2.00)   # L3 only
    assert _group(rollup, 501)["cost_captured"] == pytest.approx(0.50)   # L1 only


def test_by_issue_estimated_cost(board_rows, pricing, today_window):
    rollup = aggregate_(board_rows, by="issue", window=today_window, pricing=pricing)
    # #502: only L4 gets an estimate (L3 captured, L5 unknown model, L6 no tokens).
    assert _group(rollup, 502)["cost_est"] == pytest.approx(EST_L4)
    # #501: only L2 (L1 keeps its captured cost — never double-counted).
    assert _group(rollup, 501)["cost_est"] == pytest.approx(EST_L2)


def test_by_issue_total_cost(board_rows, pricing, today_window):
    rollup = aggregate_(board_rows, by="issue", window=today_window, pricing=pricing)
    assert _group(rollup, 502)["cost_total"] == pytest.approx(2.00 + EST_L4)   # 3.4520
    assert _group(rollup, 501)["cost_total"] == pytest.approx(0.50 + EST_L2)   # 1.4060


def test_by_issue_token_sums(board_rows, pricing, today_window):
    rollup = aggregate_(board_rows, by="issue", window=today_window, pricing=pricing)
    g502 = _group(rollup, 502)["tokens"]
    assert g502["output"] == 310_000                 # 200k+80k+30k+0
    assert g502["cache_read"] == 3_100_000           # 2.0M+0.8M+0.3M+0
    assert g502["input"] == 25_000                   # 20k+4k+1k+0
    assert g502["cache_creation"] == 0
    g501 = _group(rollup, 501)["tokens"]
    assert g501["output"] == 150_000                 # 100k+50k
    assert g501["cache_read"] == 1_500_000           # 1.0M+0.5M
    assert g501["input"] == 12_000                   # 10k+2k


def test_by_issue_duration_sums(board_rows, pricing, today_window):
    rollup = aggregate_(board_rows, by="issue", window=today_window, pricing=pricing)
    assert _group(rollup, 502)["duration_secs"] == pytest.approx(1800.0)  # 1200+400+200+0 = 30m
    assert _group(rollup, 501)["duration_secs"] == pytest.approx(900.0)   # 600+300 = 15m


def test_by_issue_open_leg_count(board_rows, pricing, today_window):
    rollup = aggregate_(board_rows, by="issue", window=today_window, pricing=pricing)
    assert _group(rollup, 502)["open_legs"] == 1     # L6 running
    assert _group(rollup, 501)["open_legs"] == 0


def test_by_issue_unknown_model_flag(board_rows, pricing, today_window):
    rollup = aggregate_(board_rows, by="issue", window=today_window, pricing=pricing)
    # L5 has an unmapped model → flagged, never silently $0.
    assert _group(rollup, 502)["unknown_models"] == 1
    assert _group(rollup, 501)["unknown_models"] == 0


def test_by_issue_sort_desc_by_total(board_rows, pricing, today_window):
    rollup = aggregate_(board_rows, by="issue", window=today_window, pricing=pricing)
    # Default sort: desc by total (captured+est). #502 (3.4520) before #501 (1.4060).
    assert [g["key"] for g in rollup["groups"]] == [502, 501]


def test_by_issue_totals_line(board_rows, pricing, today_window):
    rollup = aggregate_(board_rows, by="issue", window=today_window, pricing=pricing)
    t = rollup["totals"]
    assert t["cost_captured"] == pytest.approx(2.50)              # 0.50 + 2.00
    assert t["cost_est"] == pytest.approx(EST_L2 + EST_L4)        # 2.3580
    assert t["cost_total"] == pytest.approx(2.50 + EST_L2 + EST_L4)  # 4.8580
    assert t["tokens"]["output"] == 460_000                      # 150k + 310k
    assert t["tokens"]["cache_read"] == 4_600_000                # 1.5M + 3.1M
    assert t["duration_secs"] == pytest.approx(2700.0)           # 45m00s
    assert t["open_legs"] == 1                                   # 1 in progress


# ---------------------------------------------------------------------------
# issue drill (per-stage / per-leg) — the rows behind Mock 2
# ---------------------------------------------------------------------------

def test_issue_drill_retains_all_legs_in_order(board_rows, pricing, today_window):
    rollup = aggregate_(board_rows, by="issue", window=today_window, pricing=pricing)
    legs = _group(rollup, 502)["rows"]
    # Mock 2 order: work(opus) / smoke(sonnet) / chat(unknown) / work(sonnet running).
    assert [(r["type"], r["model"]) for r in legs] == [
        ("work", "opus"),
        ("smoke", "sonnet"),
        ("chat", "(unknown)"),
        ("work", "sonnet"),
    ]


def test_issue_drill_per_leg_estimates(board_rows, pricing):
    # The per-leg captured/est split that Mock 2 renders. Computed via the
    # public estimator so the drill test doesn't depend on how the group
    # annotates retained rows.
    by_type = {(r["type"], r["model"]): r for r in board_rows if r["issue_number"] == 502}

    # work/opus: captured cost, no estimate fires.
    assert by_type[("work", "opus")]["cost_usd"] == pytest.approx(2.00)

    # smoke/sonnet: null cost + tokens → estimate.
    assert estimate_leg_cost_(by_type[("smoke", "sonnet")], pricing) == pytest.approx(EST_L4)

    # chat/(unknown): unmapped model → no estimate (None), never $0.
    assert estimate_leg_cost_(by_type[("chat", "(unknown)")], pricing) is None

    # work/sonnet running: 0 tokens → estimate is not a positive number.
    assert (estimate_leg_cost_(by_type[("work", "sonnet")], pricing) or 0.0) == pytest.approx(0.0)


def test_issue_drill_per_leg_duration_and_running(board_rows, pricing, today_window):
    rollup = aggregate_(board_rows, by="issue", window=today_window, pricing=pricing)
    legs = _group(rollup, 502)["rows"]
    durs = {r["type"] + ":" + str(r["model"]): _leg_duration(r) for r in legs}
    assert durs["work:opus"] == pytest.approx(1200.0)     # 20m00s
    assert durs["smoke:sonnet"] == pytest.approx(400.0)   # 6m40s
    assert durs["chat:(unknown)"] == pytest.approx(200.0) # 3m20s
    # Running leg (no finished_at) contributes 0 duration.
    running = [r for r in legs if r["finished_at"] is None]
    assert len(running) == 1
    assert _leg_duration(running[0]) == pytest.approx(0.0)


def _leg_duration(row):
    """Per-leg duration per the contract: max(0, finished - dispatched); a
    running leg (no finished_at) is 0."""
    if row.get("finished_at") is None:
        return 0.0
    return max(0.0, float(row["finished_at"]) - float(row["dispatched_at"]))


# ---------------------------------------------------------------------------
# estimator unit tests
# ---------------------------------------------------------------------------

def test_estimator_exact_sonnet_l2(pricing):
    row = {"model": "sonnet", "input_tokens": 2_000, "output_tokens": 50_000,
           "cache_read_tokens": 500_000, "cache_creation_tokens": 0, "cost_usd": None}
    assert estimate_leg_cost_(row, pricing) == pytest.approx(EST_L2)


def test_estimator_exact_sonnet_l4(pricing):
    row = {"model": "sonnet", "input_tokens": 4_000, "output_tokens": 80_000,
           "cache_read_tokens": 800_000, "cache_creation_tokens": 0, "cost_usd": None}
    assert estimate_leg_cost_(row, pricing) == pytest.approx(EST_L4)


def test_estimator_includes_cache_creation(pricing):
    # cache_creation is a priced dimension too (sonnet rate 3.75 / 1M).
    row = {"model": "sonnet", "input_tokens": 0, "output_tokens": 0,
           "cache_read_tokens": 0, "cache_creation_tokens": 1_000_000, "cost_usd": None}
    assert estimate_leg_cost_(row, pricing) == pytest.approx(3.75)


def test_estimator_opus_all_dimensions(pricing):
    row = {"model": "opus", "input_tokens": 1_000_000, "output_tokens": 1_000_000,
           "cache_read_tokens": 1_000_000, "cache_creation_tokens": 1_000_000, "cost_usd": None}
    # 15 + 75 + 1.5 + 18.75
    assert estimate_leg_cost_(row, pricing) == pytest.approx(110.25)


def test_estimator_unknown_model_returns_none(pricing):
    row = {"model": "(unknown)", "input_tokens": 1_000, "output_tokens": 30_000,
           "cache_read_tokens": 300_000, "cache_creation_tokens": 0, "cost_usd": None}
    assert estimate_leg_cost_(row, pricing) is None


def test_estimator_empty_model_returns_none(pricing):
    row = {"model": "", "input_tokens": 1_000, "output_tokens": 1_000,
           "cache_read_tokens": 0, "cache_creation_tokens": 0, "cost_usd": None}
    assert estimate_leg_cost_(row, pricing) is None


def test_estimator_alias_normalizes_to_sonnet(pricing):
    aliased = {"model": "claude-sonnet-4-6", "input_tokens": 2_000, "output_tokens": 50_000,
               "cache_read_tokens": 500_000, "cache_creation_tokens": 0, "cost_usd": None}
    assert estimate_leg_cost_(aliased, pricing) == pytest.approx(EST_L2)


def test_canonical_model_alias_normalization():
    assert canonical_model_("claude-sonnet-4-6") == "sonnet"
    assert canonical_model_("sonnet") == "sonnet"
    assert canonical_model_("opus") == "opus"
    assert canonical_model_("haiku") == "haiku"


def test_canonical_model_unknown_and_empty_not_a_priced_key(pricing):
    # Whatever canonical key these normalize to, it must NOT collide with a
    # priced model — the contract requires unmapped/unknown → no estimate.
    for name in ("(unknown)", "", None):
        assert canonical_model_(name) not in pricing


def test_estimator_no_double_count_when_cost_captured(board_rows, pricing, today_window):
    # A leg with a real captured cost must not ALSO be estimated at the
    # aggregate level. L1 (captured $0.50, tokens>0) sits in #501 alongside
    # L2 (estimated). If L1 were double-counted, cost_est would exceed L2's
    # estimate. Pin it exactly to L2 → captured legs never contribute to est.
    rollup = aggregate_(board_rows, by="issue", window=today_window, pricing=pricing)
    g501 = _group(rollup, 501)
    assert g501["cost_est"] == pytest.approx(EST_L2)
    assert g501["cost_captured"] == pytest.approx(0.50)
    assert g501["cost_total"] == pytest.approx(0.50 + EST_L2)


# ---------------------------------------------------------------------------
# window predicate — half-open [start, end), dispatched OR finished in-window
# ---------------------------------------------------------------------------

def _row(dispatched, finished):
    return {"dispatched_at": dispatched, "finished_at": finished}


def test_window_predicate_dispatched_in_finished_out():
    w = Window_(100.0, 200.0)
    # dispatched inside, finished after the window end → in (OR predicate).
    assert in_window_(_row(150.0, 250.0), w) is True


def test_window_predicate_finished_in_dispatched_out():
    w = Window_(100.0, 200.0)
    # dispatched before window, finished inside → in (OR predicate).
    assert in_window_(_row(50.0, 150.0), w) is True


def test_window_predicate_both_outside():
    w = Window_(100.0, 200.0)
    assert in_window_(_row(10.0, 20.0), w) is False       # both before
    assert in_window_(_row(300.0, 400.0), w) is False     # both after


def test_window_boundary_is_half_open():
    w = Window_(100.0, 200.0)
    # start is inclusive.
    assert in_window_(_row(100.0, None), w) is True
    # end is exclusive.
    assert in_window_(_row(200.0, None), w) is False


def test_window_running_leg_matches_on_dispatch():
    w = Window_(100.0, 200.0)
    # A running leg (no finished_at) is in-window iff its dispatch is in-window.
    assert in_window_(_row(150.0, None), w) is True
    assert in_window_(_row(50.0, None), w) is False


def test_window_explicit_bounded_range_filters_aggregate(board_rows, pricing):
    # Explicit half-open range that includes only L1,L2,L3 (dispatched at
    # BASE+0, +1000, +2000) and excludes L4,L5,L6 (dispatched >= BASE+4000).
    from conftest import BASE

    w = Window_(BASE, BASE + 3_000.0)
    rollup = aggregate_(board_rows, by="issue", window=w, pricing=pricing)
    # #501 keeps both legs; #502 keeps only L3.
    assert _group(rollup, 501)["legs"] == 2
    assert _group(rollup, 502)["legs"] == 1
    assert _group(rollup, 502)["cost_captured"] == pytest.approx(2.00)  # L3
    assert _group(rollup, 502)["open_legs"] == 0                        # L6 excluded


def test_since_preset_relative_duration_days():
    # `since=<Nd>` resolves to a half-open interval ending "now" spanning N
    # days. Width is a fixed duration (no calendar/DST ambiguity), so it is
    # deterministic regardless of the wall clock.
    w = Window_since_("2d")
    assert (w.end - w.start) == pytest.approx(2 * 86_400.0)


def test_since_preset_relative_duration_hours():
    w = Window_since_("3h")
    assert (w.end - w.start) == pytest.approx(3 * 3_600.0)


def test_since_preset_iso_start():
    # `since=<ISO>` pins the start to the parsed instant; end is "now" (> start).
    w = Window_since_("2026-07-01T00:00:00")
    assert w.start < w.end
    # TODO(test-author): contract says "accept ISO" for `since` but does not
    # pin the timezone assumption for a naive ISO string (local vs UTC). We
    # only assert the ordering invariant here, not the absolute epoch value.


def test_preset_today_is_half_open_interval():
    # A preset resolves into a half-open [start, end) with start < end.
    w = Window_today_()
    assert w.start < w.end


# ---------------------------------------------------------------------------
# thin adapters so the imports fail loudly (RED) until #1118 lands, and so a
# rename of the pure module is a one-line change in this suite.
# ---------------------------------------------------------------------------

def aggregate_(*a, **k):
    from coord.usage_rollup import aggregate
    return aggregate(*a, **k)


def estimate_leg_cost_(*a, **k):
    from coord.usage_rollup import estimate_leg_cost
    return estimate_leg_cost(*a, **k)


def canonical_model_(*a, **k):
    from coord.usage_rollup import canonical_model
    return canonical_model(*a, **k)


def in_window_(*a, **k):
    from coord.usage_rollup import in_window
    return in_window(*a, **k)


def Window_(*a, **k):
    from coord.usage_rollup import Window
    return Window(*a, **k)


def Window_since_(spec):
    from coord.usage_rollup import Window
    return Window.since(spec)


def Window_today_():
    from coord.usage_rollup import Window
    return Window.today()
