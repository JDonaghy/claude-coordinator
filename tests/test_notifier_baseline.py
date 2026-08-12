"""#1632: stratified duration baselines — the "far too long" definition.

The rules under test are the ones the issue calls out as the hard part:
stratify or the average is meaningless, and cold start is a real state
rather than an edge case.
"""

from __future__ import annotations

import pytest

from coord.notifier.baseline import (
    MIN_SAMPLES,
    UNTIERED,
    Stratum,
    baseline_for,
    build_baselines,
    cold_ceiling,
    collect_samples,
    median,
    percentile,
    tier_from_labels,
)


def _row(repo="coord", type_="work", issue=1, dispatched=0.0, secs=600.0, status="done"):
    return {
        "repo_name": repo,
        "type": type_,
        "issue_number": issue,
        "status": status,
        "dispatched_at": dispatched,
        "finished_at": dispatched + secs,
    }


# ── tier extraction ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "labels,expected",
    [
        (None, UNTIERED),
        ([], UNTIERED),
        (["bug", "tier:large"], "large"),
        (["Tier:Small"], "small"),
        (["priority:high"], UNTIERED),
        # A malformed "tier:" with nothing after it is not a tier.
        (["tier:"], UNTIERED),
    ],
)
def test_tier_from_labels(labels, expected):
    assert tier_from_labels(labels) == expected


def test_tier_is_stable_regardless_of_label_order():
    """Two tier labels on one issue must not make the bucket depend on the
    order GitHub happened to return them in."""
    assert tier_from_labels(["tier:large", "tier:small"]) == tier_from_labels(
        ["tier:small", "tier:large"]
    )


# ── percentile maths ──────────────────────────────────────────────────────


def test_percentile_endpoints_and_interpolation():
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(values, 0) == 10.0
    assert percentile(values, 100) == 50.0
    assert median(values) == 30.0
    assert percentile(values, 90) == pytest.approx(46.0)


def test_percentile_of_empty_population_raises():
    with pytest.raises(ValueError):
        percentile([], 90)


# ── stratification ────────────────────────────────────────────────────────


def test_samples_are_stratified_by_repo_type_and_tier():
    labels = {("coord", 1): ["tier:large"], ("coord", 2): ["tier:small"]}
    rows = [
        _row(issue=1, secs=100.0),
        _row(issue=2, secs=200.0),
        _row(issue=1, type_="review", secs=300.0),
        _row(repo="vimcode", issue=1, secs=400.0),
    ]
    buckets = collect_samples(rows, labels_by_issue=labels)
    assert buckets[Stratum("coord", "work", "large")] == [100.0]
    assert buckets[Stratum("coord", "work", "small")] == [200.0]
    assert buckets[Stratum("coord", "review", "large")] == [300.0]
    # vimcode#1 has no cached labels -> its own `untiered` bucket, not
    # silently folded in with coord's `tier:large` population.
    assert buckets[Stratum("vimcode", "work", UNTIERED)] == [400.0]


def test_open_legs_are_not_samples():
    """A still-running leg has no duration yet; counting it as zero would
    drag every baseline toward "everything is slow"."""
    rows = [_row(secs=600.0) for _ in range(MIN_SAMPLES)]
    rows.append({"repo_name": "coord", "type": "work", "issue_number": 9,
                 "status": "running", "dispatched_at": 0.0, "finished_at": None})
    buckets = collect_samples(rows)
    assert len(buckets[Stratum("coord", "work", UNTIERED)]) == MIN_SAMPLES


def test_failed_legs_are_still_duration_samples():
    """The question is how long this KIND of work takes, not how often it
    works — dropping failures would bias the baseline short."""
    rows = [_row(secs=600.0, status="failed") for _ in range(MIN_SAMPLES)]
    baselines = build_baselines(rows)
    assert not baselines[Stratum("coord", "work", UNTIERED)].cold


# ── cold start ────────────────────────────────────────────────────────────


def test_under_min_samples_is_cold_and_uses_the_absolute_ceiling():
    rows = [_row(secs=60.0) for _ in range(MIN_SAMPLES - 1)]
    base = build_baselines(rows)[Stratum("coord", "work", UNTIERED)]
    assert base.cold is True
    assert base.samples == MIN_SAMPLES - 1
    assert base.percentile_secs is None
    assert base.duration_threshold == cold_ceiling("work")
    # And it says so, out loud, in the text that reaches the phone.
    assert "no baseline yet" in base.basis()
    assert f"{MIN_SAMPLES}" in base.basis()


def test_population_of_one_never_produces_a_baseline():
    base = build_baselines([_row(secs=1.0)])[Stratum("coord", "work", UNTIERED)]
    assert base.cold is True
    # A 1-second population must not make a 2-second job look pathological.
    assert base.duration_threshold >= 3600.0


def test_unknown_stratum_behaves_exactly_like_a_cold_one():
    base = baseline_for({}, Stratum("brand-new", "work", "large"))
    assert base.cold is True
    assert base.samples == 0
    assert base.duration_threshold == cold_ceiling("work")


def test_cold_ceiling_is_per_assignment_type():
    assert cold_ceiling("review") < cold_ceiling("work")
    assert cold_ceiling("a-type-nobody-has-heard-of") > 0


# ── warm baselines ────────────────────────────────────────────────────────


def test_warm_baseline_reports_p90_and_the_2x_median_alternative():
    """#1632 asks for p90 *and* 2x median so the two can be compared
    against real data — computing only the chosen one makes that
    impossible."""
    rows = [_row(secs=s) for s in (600, 600, 600, 600, 600, 600, 600, 600, 600, 6000)]
    base = build_baselines(rows)[Stratum("coord", "work", UNTIERED)]
    assert base.cold is False
    assert base.median_secs == pytest.approx(600.0)
    assert base.p2x_median_secs == pytest.approx(1200.0)
    assert base.percentile_secs > base.median_secs
    assert base.duration_threshold == base.percentile_secs
    assert "p90 of 10 comparable" in base.basis()


def test_silence_threshold_scales_with_the_stratum_and_is_clamped():
    """A repo whose legs are long is a repo whose quiet spells are long —
    but a fixed value in either direction would spam or never fire."""
    fast = build_baselines([_row(secs=60.0) for _ in range(MIN_SAMPLES)])
    slow = build_baselines(
        [_row(repo="vimcode", secs=6 * 3600.0) for _ in range(MIN_SAMPLES)]
    )
    fast_base = fast[Stratum("coord", "work", UNTIERED)]
    slow_base = slow[Stratum("vimcode", "work", UNTIERED)]
    # Clamped at both ends rather than proportional all the way down/up.
    assert fast_base.silence_threshold == 10 * 60.0
    assert slow_base.silence_threshold == 45 * 60.0
    assert fast_base.silence_threshold < slow_base.silence_threshold


def test_observed_silence_samples_take_precedence_when_available():
    """The derived-from-duration silence threshold is an approximation with
    a documented upgrade path; passing real samples must use them."""
    stratum = Stratum("coord", "work", UNTIERED)
    rows = [_row(secs=600.0) for _ in range(MIN_SAMPLES)]
    base = build_baselines(
        rows, silence_samples={stratum: [100.0, 110.0, 120.0, 130.0, 140.0]}
    )[stratum]
    assert base.silence_threshold == pytest.approx(percentile(
        [100.0, 110.0, 120.0, 130.0, 140.0], 90.0
    ))


def test_configured_percentile_is_honoured():
    rows = [_row(secs=float(s)) for s in range(100, 100 + 20)]
    p50 = build_baselines(rows, percentile_q=50.0)[Stratum("coord", "work", UNTIERED)]
    p99 = build_baselines(rows, percentile_q=99.0)[Stratum("coord", "work", UNTIERED)]
    assert p50.duration_threshold < p99.duration_threshold
