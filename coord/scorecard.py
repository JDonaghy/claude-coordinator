"""Dogfood scorecard (#1559): turn a milestone into evidence, not vibes.

``docs/WEB_CONTROL_CENTER.md`` names five metrics the M-W0/M-W1 dogfood
program is supposed to answer with numbers rather than "it felt like it
worked": first-pass acceptance rate, human interventions (count + kind),
cost + wall-clock per issue, escaped defects by stage, and process bugs
surfaced. This module is the pure aggregator that turns three already-
fetched inputs — a milestone's GitHub issues (with labels), the repo's board
assignment rows, and (optionally) the audit trail — into per-issue and
per-milestone numbers.

Split the same way :mod:`coord.usage_rollup` is split from :mod:`coord.usage`:
this module takes plain data in (``dict``s matching the daemon ``/board``
wire shape, the GitHub CLI's issue-list JSON shape, and the audit log's
entry shape) and returns plain data out. No I/O, no ``gh``, no daemon calls.
The *fetch* side lives in :mod:`coord.commands.scorecard`, kept separate so
this module is unit-testable against fixtures with no GitHub/daemon
reachable — the point of #1559's "validate against history" acceptance
criterion.

## The two label conventions

Three of the five metrics (first-pass acceptance, interventions, cost/time)
read straight off data coord already records — see the per-function
docstrings below for exactly which board columns. The other two have no
automatic signal and need a labelling convention cheap enough to survive
contact with a real program; both apply via the existing ``coord issue
label <repo> <issue> --add <label>`` command, in one call, at the moment of
discovery — no new CLI surface needed.

**Escaped defects** — apply ``escaped:<stage>`` where ``<stage>`` is one of
:data:`ESCAPED_DEFECT_STAGES` (``review``, ``gate-b``, ``post-merge``,
``live``), naming the stage that *should* have caught the defect. An issue
with none of these labels reports as "unlabeled", not "zero" — see
:func:`build_milestone_scorecard`'s ``escaped_defects.issues_unlabeled``.

**Process bugs** — apply ``process-bug`` to a bug filed against coord itself
under the *same* milestone as the program that surfaced it (so this module
never needs a second GitHub call to find them). Once the fix lands, apply
either ``regression-test:landed`` or ``regression-test:missing``. A
``process-bug`` issue carrying neither sub-label reports ``regression_test
= "unknown"`` — still triaging, not "no test".

## First-pass acceptance and intervention kind — the board convention

An issue's *root* work assignment is a ``type in ("work", "test-author")``
row with no ``review_of_assignment_id`` and ``review_iteration`` 0 (see
:func:`is_root_work`). Every follow-up dispatched against that root —
whether ``coord assign --interactive --fix-of``/``--rework-of`` (human) or
``auto_loop``'s headless review→fix bounce (automated) — keeps
``type="work"`` and sets ``review_of_assignment_id`` to the assignment id of
whatever it's a follow-up *to*, which is the ROOT's id only for the first
fix/rework iteration dispatched directly off the root's review. A review
dispatched against a completed FIX (itself ``type="work"``, eligible for
review the same as the root — see ``review.py:dispatch_review``'s
``review_of_assignment_id=completed.assignment_id``) sets its own
``review_of_assignment_id`` to that fix's id, not the root's; a second
fix/rework chained off *that* review therefore points at the immediately
preceding iteration, not the root (see
``dispatch_workers.py:_dispatch_fix_of``/``_dispatch_rework_of`` — both
resolve ``work`` from the review's own ``review_of_assignment_id``, which
may itself be a fix — and ``auto_loop.py``'s equivalent bounce-fix dispatch,
same "whatever the review points at" resolution). This doesn't confuse
:func:`is_root_work` or :func:`classify_intervention` below — both only
check whether ``review_of_assignment_id`` is *set*, never its target, so a
row anywhere in a multi-iteration chain is correctly never mistaken for a
root and always correctly counted as a follow-up. Every follow-up in the
chain, human or automated, does prefix ``issue_title`` with ``"[fix-N] "``
(``auto_loop.py`` uses that exact tag too, so title alone can't tell them
apart). What DOES distinguish a human follow-up is
``provider_name == "claude-pty"`` — the same signal
``coord.reconcile.is_interactive_merge_session`` and
``coord.config.Config.attention_threshold_for`` already key off of for
"is this an interactive session", set only by the interactive dispatch
front doors, never by ``auto_loop``'s HTTP ``/assign`` POST to the headless
agent host. ``"[rework-N] "`` (only ever set by the human ``--rework-of``
front door) further splits "fix" from "rescue" — see
:func:`classify_intervention`.

"First-pass acceptance" (:func:`build_milestone_scorecard`'s
``first_pass``) is therefore: **exactly one root work assignment, reaching
``status="merged"``, with zero human fix/rework descendants.** An issue
with no board data at all (never dispatched through coord, or the data
didn't survive) reports ``"unknown"`` — never lumped in with ``"no"``.

A second root (e.g. ``coord retry`` dispatching a fresh, unlinked
``type="work"`` row with no ``review_of_assignment_id`` after a failure,
possibly on a different machine) also fails ``first_pass``, but doesn't fit
any of ``interventions.by_kind`` — it's not a fix/rescue/nudge/abandon, it's
a second unrelated attempt. That case is its own signal,
:attr:`IssueScorecard.multiple_roots` / the totals'
``interventions.issues_with_multiple_roots``, so ``first_pass="no"`` with
zero interventions counted doesn't read as a mystery.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = [
    "ESCAPED_DEFECT_STAGES",
    "INTERVENTION_KINDS",
    "PROCESS_BUG_LABEL",
    "REGRESSION_TEST_LANDED_LABEL",
    "REGRESSION_TEST_MISSING_LABEL",
    "IssueScorecard",
    "MilestoneScorecard",
    "classify_intervention",
    "is_root_work",
    "build_milestone_scorecard",
    "scorecard_to_dict",
]

# ── Label conventions ───────────────────────────────────────────────────────

ESCAPED_DEFECT_STAGES = ("review", "gate-b", "post-merge", "live")
_ESCAPED_LABEL_RE = re.compile(
    r"^escaped:(" + "|".join(re.escape(s) for s in ESCAPED_DEFECT_STAGES) + r")$"
)

PROCESS_BUG_LABEL = "process-bug"
REGRESSION_TEST_LANDED_LABEL = "regression-test:landed"
REGRESSION_TEST_MISSING_LABEL = "regression-test:missing"

# ── Intervention kinds ───────────────────────────────────────────────────────

INTERVENTION_KINDS = ("fix", "rescue", "nudge", "abandon")

# Assignment types a human --fix-of/--rework-of dispatch always keeps (see
# dispatch_workers.py:_dispatch_fix_of/_dispatch_rework_of, and auto_loop.py's
# fix_type="test-author" carve-out for a test-author slice fix).
_ROOT_WORK_TYPES = frozenset({"work", "test-author"})

# Human-attended, per-issue conversational sessions (coord.config.
# INTERACTIVE_SESSION_TYPES minus the milestone/pre-creation-scoped members
# that don't attach to one issue's execution: "audit", "milestone-chat",
# "new-issue-chat").
_NUDGE_TYPES = frozenset({"chat", "troubleshoot", "test-chat", "refinement"})


def is_root_work(row: dict) -> bool:
    """Whether *row* is an issue's original (non-fix, non-rework) dispatch.

    ``type`` in :data:`_ROOT_WORK_TYPES`, no ``review_of_assignment_id``, and
    ``review_iteration`` 0 (missing/``None`` counts as 0 — predates #1176).
    """
    if str(row.get("type") or "work") not in _ROOT_WORK_TYPES:
        return False
    if row.get("review_of_assignment_id"):
        return False
    try:
        iteration = int(row.get("review_iteration") or 0)
    except (TypeError, ValueError):
        iteration = 0
    return iteration == 0


def classify_intervention(row: dict) -> str | None:
    """Classify one board assignment row as a human-intervention kind.

    Returns ``None`` for anything that isn't one — including ``auto_loop``'s
    headless bounce-fix dispatch, which shares ``review_of_assignment_id``/
    ``review_iteration``/``"[fix-N] "``-title shape with a human fix but
    never sets ``provider_name="claude-pty"`` (only the interactive dispatch
    front doors in ``dispatch_workers.py`` do — see module docstring), and a
    mechanical ``type="conflict-fix"`` automated merge repair
    (``conflict_fix.py`` — the coordinator's own automatic
    mechanical-conflict resolution, not a human touching anything, and it
    never sets ``provider_name="claude-pty"`` either).

    ``"abandon"`` is NOT classified here — it's a per-issue fact (closed,
    board data exists, nothing ever merged), not a single assignment's
    shape; see :func:`build_milestone_scorecard`.
    """
    rtype = str(row.get("type") or "work")
    provider = str(row.get("provider_name") or "")
    title = str(row.get("issue_title") or "")
    if (
        rtype in _ROOT_WORK_TYPES
        and row.get("review_of_assignment_id")
        and provider == "claude-pty"
    ):
        if title.startswith("[rework-"):
            return "rescue"
        # "[fix-N] " (the interactive --fix-of front door) and any other
        # human follow-up shape without a recognized title tag both land
        # here — "fix" is the more common of the two human flavours.
        return "fix"
    if rtype in _NUDGE_TYPES:
        return "nudge"
    return None


def _labels_of(issue: dict) -> set[str]:
    out: set[str] = set()
    for entry in issue.get("labels") or []:
        name = entry.get("name") if isinstance(entry, dict) else entry
        if name:
            out.add(str(name))
    return out


def _escaped_defect_stage(labels: set[str]) -> str | None:
    for label in labels:
        m = _ESCAPED_LABEL_RE.match(label)
        if m:
            return m.group(1)
    return None


def _process_bug_fields(labels: set[str]) -> tuple[bool, str | None]:
    if PROCESS_BUG_LABEL not in labels:
        return False, None
    if REGRESSION_TEST_LANDED_LABEL in labels:
        return True, "landed"
    if REGRESSION_TEST_MISSING_LABEL in labels:
        return True, "missing"
    return True, "unknown"


# ── Per-issue / per-milestone result shapes ─────────────────────────────────


@dataclass
class IssueScorecard:
    """The five metrics resolved for one issue."""

    number: int
    title: str
    state: str  # "OPEN" | "CLOSED" | "" (unknown)

    # First-pass acceptance: "yes" | "no" | "unknown" (no board data at all).
    first_pass: str

    # Count per INTERVENTION_KINDS, always all four keys present (0, never
    # omitted) once has_assignment_data is True.
    interventions: dict[str, int]
    has_assignment_data: bool

    # True when more than one root work assignment exists for this issue
    # (e.g. `coord retry` dispatching a fresh unlinked `type="work"` row
    # after a failure, possibly on a different machine) — see
    # `build_milestone_scorecard`'s docstring. This is why `first_pass` can
    # be "no" with all four `interventions` counts at 0: none of
    # fix/rescue/nudge/abandon fit "a second, unrelated root appeared."
    multiple_roots: bool

    # Cost + wall-clock, from coord.usage_rollup.aggregate(by="issue").
    cost_captured: float
    cost_est: float
    cost_total: float
    duration_secs: float
    legs: int
    has_cost_data: bool

    # Escaped defects / process bugs — label-sourced, see module docstring.
    escaped_defect_stage: str | None
    process_bug: bool
    regression_test: str | None  # "landed" | "missing" | "unknown" | None


@dataclass
class MilestoneScorecard:
    """The full report for one milestone: per-issue rows + the aggregate."""

    milestone: Any
    milestone_title: str
    repo_name: str
    issues: list[IssueScorecard] = field(default_factory=list)
    totals: dict = field(default_factory=dict)


def _aggregate_totals(cards: list[IssueScorecard]) -> dict:
    fp_yes = sum(1 for c in cards if c.first_pass == "yes")
    fp_no = sum(1 for c in cards if c.first_pass == "no")
    fp_unknown = sum(1 for c in cards if c.first_pass == "unknown")
    fp_decided = fp_yes + fp_no
    fp_rate = (fp_yes / fp_decided) if fp_decided else None

    by_kind = {
        k: sum(c.interventions.get(k, 0) for c in cards) for k in INTERVENTION_KINDS
    }
    issues_with_unknown_data = sum(1 for c in cards if not c.has_assignment_data)
    issues_with_multiple_roots = sum(1 for c in cards if c.multiple_roots)

    cost_total = sum(c.cost_total for c in cards)
    cost_captured = sum(c.cost_captured for c in cards)
    cost_est = sum(c.cost_est for c in cards)
    duration_total = sum(c.duration_secs for c in cards)
    issues_with_cost_data = sum(1 for c in cards if c.has_cost_data)

    escaped_by_stage = {
        s: sum(1 for c in cards if c.escaped_defect_stage == s)
        for s in ESCAPED_DEFECT_STAGES
    }
    escaped_unlabeled = sum(1 for c in cards if c.escaped_defect_stage is None)

    process_bugs = [c for c in cards if c.process_bug]

    return {
        "issue_count": len(cards),
        "first_pass": {
            "yes": fp_yes,
            "no": fp_no,
            "unknown": fp_unknown,
            "rate": fp_rate,
        },
        "interventions": {
            "by_kind": by_kind,
            "total": sum(by_kind.values()),
            "issues_with_unknown_data": issues_with_unknown_data,
            # first_pass="no" with all four by_kind counts at 0 means one of
            # these — a second, unrelated root dispatch, not a fix/rescue/
            # nudge/abandon. See IssueScorecard.multiple_roots.
            "issues_with_multiple_roots": issues_with_multiple_roots,
        },
        "cost": {
            "total_usd": cost_total,
            "captured_usd": cost_captured,
            "estimated_usd": cost_est,
            "duration_secs": duration_total,
            "issues_with_data": issues_with_cost_data,
            "issues_without_data": len(cards) - issues_with_cost_data,
        },
        "escaped_defects": {
            "by_stage": escaped_by_stage,
            "issues_unlabeled": escaped_unlabeled,
        },
        "process_bugs": {
            "count": len(process_bugs),
            "regression_test": {
                "landed": sum(1 for c in process_bugs if c.regression_test == "landed"),
                "missing": sum(1 for c in process_bugs if c.regression_test == "missing"),
                "unknown": sum(1 for c in process_bugs if c.regression_test == "unknown"),
            },
        },
    }


def _score_one_issue(
    *, issue: dict, rows: list[dict], cost_group: dict | None, audited_merged: bool
) -> IssueScorecard:
    labels = _labels_of(issue)
    escaped_stage = _escaped_defect_stage(labels)
    process_bug, regression_test = _process_bug_fields(labels)

    roots = [r for r in rows if is_root_work(r)]
    counts = {k: 0 for k in INTERVENTION_KINDS}
    for row in rows:
        kind = classify_intervention(row)
        if kind:
            counts[kind] += 1

    has_data = bool(rows)
    merged = audited_merged or any(str(r.get("status")) == "merged" for r in roots)
    human_followups = counts["fix"] + counts["rescue"] + counts["nudge"]
    multiple_roots = len(roots) > 1

    if not has_data:
        first_pass = "unknown"
    elif len(roots) == 1 and merged and human_followups == 0:
        first_pass = "yes"
    else:
        first_pass = "no"

    # Abandon: closed, we have board history, but nothing ever merged — the
    # automated pipeline didn't land this; someone finished (or dropped) it
    # outside coord's tracked chain.
    state = str(issue.get("state") or "").upper()
    if has_data and state == "CLOSED" and not merged:
        counts["abandon"] = 1

    if cost_group is not None:
        cost_captured = float(cost_group.get("cost_captured") or 0.0)
        cost_est = float(cost_group.get("cost_est") or 0.0)
        cost_total = float(cost_group.get("cost_total") or (cost_captured + cost_est))
        duration_secs = float(cost_group.get("duration_secs") or 0.0)
        legs = int(cost_group.get("legs") or 0)
        has_cost_data = legs > 0
    else:
        cost_captured = cost_est = cost_total = duration_secs = 0.0
        legs = 0
        has_cost_data = False

    return IssueScorecard(
        number=int(issue["number"]),
        title=str(issue.get("title") or ""),
        state=state,
        first_pass=first_pass,
        interventions=counts,
        has_assignment_data=has_data,
        multiple_roots=multiple_roots,
        cost_captured=cost_captured,
        cost_est=cost_est,
        cost_total=cost_total,
        duration_secs=duration_secs,
        legs=legs,
        has_cost_data=has_cost_data,
        escaped_defect_stage=escaped_stage,
        process_bug=process_bug,
        regression_test=regression_test,
    )


def build_milestone_scorecard(
    *,
    milestone: Any,
    milestone_title: str,
    repo_name: str,
    issues: list[dict],
    assignment_rows: list[dict],
    audit_entries: list[dict] | None = None,
    pricing: dict | None = None,
) -> MilestoneScorecard:
    """Build the full scorecard for one milestone.

    Parameters mirror the three fetch seams :mod:`coord.commands.scorecard`
    composes: *issues* is ``coord.github_ops.get_milestone_issues``'s shape
    (``{"number", "title", "state", "labels"}``, open+closed);
    *assignment_rows* is ``coord.usage.fetch_usage_rows()``'s shape (the
    daemon ``/board`` wire format, ALL repos — this function filters to
    *repo_name* itself so callers don't have to); *audit_entries* is
    optional (``coord.state.list_audit_log`` entries) and used only as a
    durability cross-check for "merged" — a board row that never got its
    ``status`` flipped (a reconcile-sweep gap) still counts as merged if the
    audit trail recorded the merge event, so a real board-sync hiccup
    doesn't misreport an accepted issue as abandoned. *pricing* is
    ``coord.usage.pricing_dict_from_config(cfg.pricing)``'s shape; ``None``
    uses the built-in default rates (matches ``aggregate()``'s own default).
    """
    from coord.usage_rollup import Window, aggregate, row_issue_number  # noqa: PLC0415

    repo_rows = [r for r in assignment_rows if str(r.get("repo_name") or "") == repo_name]

    cost_result = aggregate(
        repo_rows, by="issue", window=Window(), pricing=pricing or {}
    )
    cost_by_issue = {g["key"]: g for g in cost_result["groups"]}

    audited_merged: set[int] = set()
    for entry in audit_entries or []:
        if entry.get("event_type") != "merged":
            continue
        if str(entry.get("repo") or "") != repo_name:
            continue
        issue_no = entry.get("issue")
        if issue_no is not None:
            audited_merged.add(int(issue_no))

    by_issue: dict[int, list[dict]] = {}
    for row in repo_rows:
        by_issue.setdefault(row_issue_number(row), []).append(row)

    cards = [
        _score_one_issue(
            issue=issue,
            rows=by_issue.get(int(issue["number"]), []),
            cost_group=cost_by_issue.get(int(issue["number"])),
            audited_merged=int(issue["number"]) in audited_merged,
        )
        for issue in issues
    ]

    return MilestoneScorecard(
        milestone=milestone,
        milestone_title=milestone_title,
        repo_name=repo_name,
        issues=cards,
        totals=_aggregate_totals(cards),
    )


def scorecard_to_dict(card: MilestoneScorecard) -> dict:
    """Plain-dict rendering for JSON output — ``dataclasses.asdict`` with no
    surprises since every field is already a JSON-safe primitive/dict/list."""
    return {
        "milestone": card.milestone,
        "milestone_title": card.milestone_title,
        "repo_name": card.repo_name,
        "issues": [asdict(c) for c in card.issues],
        "totals": card.totals,
    }
