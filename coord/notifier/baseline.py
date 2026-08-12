"""Learned duration baselines for the fleet notifier (#1632).

**Do not use a fixed timeout.**  A constant is wrong the day it is written
and rots silently as models, repos and hardware change.  Milestone #37
already ships per-leg duration on every assignment row, so this module
needs no new instrumentation — only a baseline computed over records that
are already there.

Two rules the shape of this module exists to enforce:

1. **Stratify, or the average is meaningless.**  A ``work`` leg on a
   ``tier:large`` vimcode issue and a ``review`` on a small coord issue are
   not the same population.  The key is ``(repo, assignment type, tier)``;
   an unstratified fleet-wide mean fires constantly on the slow tail and
   never on the fast one.
2. **Cold start is a real state, not an edge case.**  Under
   :data:`MIN_SAMPLES` completed legs there is *no* baseline — the notifier
   falls back to a generous absolute ceiling for that type and says so in
   the notification text.  Never fire off a population of one.

Everything here is pure: rows in, values out, no I/O and no clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from coord.usage_rollup import leg_duration

#: Below this many completed samples a stratum has no baseline at all.
#: Five is the smallest population where a p90 is not simply "the maximum",
#: and the point of the rule is that a population of one must never fire.
MIN_SAMPLES = 5

#: Which percentile of the stratified population becomes the "far too long"
#: line.  p90 is the #1632 proposal; ``2 * median`` is the documented
#: alternative and is computed alongside it on every stratum so the two can
#: be compared against real fleet data (``coord notifier baselines``)
#: before either is committed to permanently.
DEFAULT_PERCENTILE = 90.0

#: Generous absolute ceilings, per assignment type, used ONLY while a
#: stratum is cold.  Deliberately loose — a cold-start ceiling exists to
#: catch a catastrophically wedged leg, not to be accurate.
DEFAULT_COLD_CEILINGS: dict[str, float] = {
    "work": 4 * 3600.0,
    "mock-author": 4 * 3600.0,
    "test-author": 4 * 3600.0,
    "review": 90 * 60.0,
    "smoke": 90 * 60.0,
    "merge": 90 * 60.0,
    "conflict-fix": 90 * 60.0,
    "plan": 45 * 60.0,
    "audit": 90 * 60.0,
    "chat": 45 * 60.0,
}
#: Applied to any type not named above.
FALLBACK_COLD_CEILING = 4 * 3600.0

#: A stratum's silence threshold is derived from the same population as its
#: duration threshold (see :func:`build_baselines`), as this fraction of the
#: median leg, clamped into ``[SILENCE_FLOOR, SILENCE_CAP]``.
DEFAULT_SILENCE_FRACTION = 0.5
SILENCE_FLOOR_SECS = 10 * 60.0
SILENCE_CAP_SECS = 45 * 60.0
#: Silence threshold while a stratum is cold.
COLD_SILENCE_SECS = 30 * 60.0

#: Statuses that mean "this leg finished and its duration is a real sample".
#: A ``failed`` leg is still a legitimate duration observation — the notifier
#: is asking how long this kind of work *takes*, not how often it works.
SAMPLE_STATUSES = frozenset({"done", "failed", "merged", "advisory"})

#: The tier bucket for an issue carrying no ``tier:*`` label.  A real bucket,
#: not a null: untiered issues are their own population and averaging them
#: in with ``tier:large`` is exactly the stratification bug rule 1 forbids.
UNTIERED = "untiered"


def tier_from_labels(labels: Iterable[str] | None) -> str:
    """The ``tier:*`` bucket for a set of GitHub issue labels.

    Mirrors ``coord.config.ModelsConfig.model_for_labels``' tier-first
    convention (``tier:small`` / ``tier:large``), and returns
    :data:`UNTIERED` when no tier label is present.  Ties are broken by
    sorted order so the bucket is stable regardless of the order GitHub
    happened to return the labels in.
    """
    if not labels:
        return UNTIERED
    tiers = sorted(
        str(label).split(":", 1)[1].strip().lower()
        for label in labels
        if isinstance(label, str) and label.lower().startswith("tier:") and ":" in label
    )
    tiers = [t for t in tiers if t]
    return tiers[0] if tiers else UNTIERED


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile of *values* (``q`` in ``0..100``).

    Rolled by hand rather than pulled from numpy: the base install is a
    thin client (#1237) and one percentile is not worth a dependency.
    """
    if not values:
        raise ValueError("percentile() of an empty population")
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    q = max(0.0, min(100.0, float(q)))
    pos = (len(ordered) - 1) * (q / 100.0)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


def median(values: Sequence[float]) -> float:
    return percentile(values, 50.0)


@dataclass(frozen=True)
class Stratum:
    """The population a duration is judged against."""

    repo: str
    type: str
    tier: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.repo}/{self.type}/{self.tier}"


@dataclass(frozen=True)
class Baseline:
    """What the history says about one stratum.

    ``cold`` is the load-bearing field.  When it is true there is no learned
    threshold at all and :attr:`duration_threshold` is a generous absolute
    ceiling — the notification text MUST say so, because "over the ceiling
    for a stratum we have never measured" is a much weaker claim than "over
    the p90 of 40 comparable legs".
    """

    stratum: Stratum
    samples: int
    cold: bool
    #: The threshold actually used.  Learned percentile when warm, the
    #: cold-start absolute ceiling when cold.
    duration_threshold: float
    #: Seconds of no new output before the silence probe fires.
    silence_threshold: float
    #: Learned statistics, ``None`` while cold.  ``p2x_median`` is the
    #: documented alternative to ``percentile_secs``, retained so the two
    #: can be compared against real data before committing to either.
    percentile_secs: float | None = None
    median_secs: float | None = None
    p2x_median_secs: float | None = None
    #: Which percentile ``percentile_secs`` is.
    percentile_q: float = DEFAULT_PERCENTILE

    def basis(self) -> str:
        """One clause explaining where the threshold came from.

        Goes straight into the notification body — the operator has to be
        able to tell a learned verdict from a cold-start guess on a phone
        screen, without opening a terminal.
        """
        if self.cold:
            return (
                f"no baseline yet ({self.samples}/{MIN_SAMPLES} samples for "
                f"{self.stratum}) — using the generous cold-start ceiling"
            )
        return (
            f"p{self.percentile_q:g} of {self.samples} comparable "
            f"{self.stratum} legs"
        )


def cold_ceiling(assignment_type: str, ceilings: Mapping[str, float] | None = None) -> float:
    table = DEFAULT_COLD_CEILINGS if ceilings is None else ceilings
    return float(table.get(assignment_type, FALLBACK_COLD_CEILING))


def cold_baseline(
    stratum: Stratum,
    *,
    samples: int = 0,
    ceilings: Mapping[str, float] | None = None,
    cold_silence_secs: float = COLD_SILENCE_SECS,
) -> Baseline:
    """The baseline for a stratum with too little history to learn from."""
    return Baseline(
        stratum=stratum,
        samples=samples,
        cold=True,
        duration_threshold=cold_ceiling(stratum.type, ceilings),
        silence_threshold=float(cold_silence_secs),
    )


def _row_tier(row: Mapping[str, Any], labels_by_issue: Mapping[tuple[str, int], list[str]]) -> str:
    repo = str(row.get("repo_name") or "")
    issue = row.get("for_issue_number") or row.get("issue_number")
    try:
        issue_no = int(issue)
    except (TypeError, ValueError):
        return UNTIERED
    return tier_from_labels(labels_by_issue.get((repo, issue_no)))


def stratum_for_row(
    row: Mapping[str, Any],
    labels_by_issue: Mapping[tuple[str, int], list[str]] | None = None,
) -> Stratum:
    """The ``(repo, type, tier)`` population a board row belongs to."""
    return Stratum(
        repo=str(row.get("repo_name") or ""),
        type=str(row.get("type") or "work"),
        tier=_row_tier(row, labels_by_issue or {}),
    )


def collect_samples(
    rows: Iterable[Mapping[str, Any]],
    *,
    labels_by_issue: Mapping[tuple[str, int], list[str]] | None = None,
    statuses: frozenset[str] = SAMPLE_STATUSES,
) -> dict[Stratum, list[float]]:
    """Group completed-leg durations by stratum.

    Open legs (no ``finished_at``) are dropped rather than counted as zero —
    ``leg_duration`` reports them via its ``is_open`` flag precisely so a
    still-running job cannot drag a baseline down.
    """
    buckets: dict[Stratum, list[float]] = {}
    for row in rows:
        if str(row.get("status") or "") not in statuses:
            continue
        secs, is_open = leg_duration(dict(row))
        if is_open or secs <= 0:
            continue
        buckets.setdefault(stratum_for_row(row, labels_by_issue), []).append(float(secs))
    return buckets


def build_baselines(
    rows: Iterable[Mapping[str, Any]],
    *,
    labels_by_issue: Mapping[tuple[str, int], list[str]] | None = None,
    min_samples: int = MIN_SAMPLES,
    percentile_q: float = DEFAULT_PERCENTILE,
    ceilings: Mapping[str, float] | None = None,
    silence_fraction: float = DEFAULT_SILENCE_FRACTION,
    silence_samples: Mapping[Stratum, Sequence[float]] | None = None,
    cold_silence_secs: float = COLD_SILENCE_SECS,
) -> dict[Stratum, Baseline]:
    """Learn a :class:`Baseline` per stratum from historical board rows.

    ``silence_samples`` is the seam for *observed* per-leg silence gaps.
    Nothing in the fleet records those today — there is no last-output
    timestamp on an assignment row — so by default the silence threshold is
    **derived from the same duration population**: a fraction of the
    stratum's median leg, clamped into ``[SILENCE_FLOOR_SECS,
    SILENCE_CAP_SECS]``.  That is the honest approximation of the #1632
    requirement ("a repo whose test suite takes 20 minutes legitimately goes
    quiet"): a stratum whose legs are long is a stratum whose quiet spells
    are long.  When real silence samples do get instrumented, pass them here
    and they take precedence — the call sites do not change.
    """
    buckets = collect_samples(rows, labels_by_issue=labels_by_issue)
    observed_silence = dict(silence_samples or {})
    out: dict[Stratum, Baseline] = {}

    for stratum, values in buckets.items():
        if len(values) < min_samples:
            out[stratum] = cold_baseline(
                stratum,
                samples=len(values),
                ceilings=ceilings,
                cold_silence_secs=cold_silence_secs,
            )
            continue

        med = median(values)
        pct = percentile(values, percentile_q)
        silence_pop = observed_silence.get(stratum)
        if silence_pop is not None and len(silence_pop) >= min_samples:
            silence = percentile(list(silence_pop), percentile_q)
        else:
            silence = max(
                SILENCE_FLOOR_SECS, min(SILENCE_CAP_SECS, med * float(silence_fraction))
            )

        out[stratum] = Baseline(
            stratum=stratum,
            samples=len(values),
            cold=False,
            duration_threshold=pct,
            silence_threshold=silence,
            percentile_secs=pct,
            median_secs=med,
            p2x_median_secs=med * 2.0,
            percentile_q=percentile_q,
        )
    return out


def baseline_for(
    baselines: Mapping[Stratum, Baseline],
    stratum: Stratum,
    *,
    ceilings: Mapping[str, float] | None = None,
    cold_silence_secs: float = COLD_SILENCE_SECS,
) -> Baseline:
    """Look up *stratum*, synthesising a cold baseline when it is unknown.

    A stratum the fleet has never completed a leg for is the extreme cold
    case (zero samples) and must behave identically to the four-sample case:
    generous ceiling, ``cold=True``, and the notification says so.
    """
    found = baselines.get(stratum)
    if found is not None:
        return found
    return cold_baseline(
        stratum, samples=0, ceilings=ceilings, cold_silence_secs=cold_silence_secs
    )
