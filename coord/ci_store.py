"""CiStore abstraction over CI check status.

Phase 1 (#240) of the CiStore abstraction: a thin Protocol over ``gh pr checks``
so the merge gate can hard-block on failed checks and the TUI can surface what
broke. Rerun, polling, and non-GitHub backends are deferred to later phases.

Phase 2 (#1851) adds the rerun half: :meth:`CiStore.rerun_for_pr` and the
:func:`checks_are_stale` predicate. A **green** check can itself be stale —
GitHub re-runs ``pull_request`` workflows on head ``synchronize``, never on
base movement, so a passing check proves the composite passed against the
base *as of the last head push*, not as of now. Polling and non-GitHub
backends remain deferred.

Phase 3 (#1892) adds a **RED** check's own analogue of the same question:
did this failure say anything about the code at all? :meth:`CiStore.
list_jobs_for_run` and :func:`is_verdictless_job` distinguish "never assigned
a runner" / "died before checkout" (a statement about the CI *platform*) from
a genuine test failure (a statement about the *code*) — used exclusively by
:mod:`coord.merge_queue`'s drive-retry accounting, never by the merge gate
itself (:func:`failed_checks` and ``_PASSING_CONCLUSIONS`` are unchanged: a
verdictless check still blocks the merge, it just doesn't cost a retry).

The split between :class:`CiStore` (Protocol) and the concrete backends
(:class:`coord.ci_github.GitHubCi`, :class:`NoOpCi`) means tests can pass a
stub through ``ci_store=`` without touching subprocess at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class CiCheckSummary:
    """Structured rollup of a PR's CI checks — the board-wire analogue of the
    TUI's Rust ``CiCheckSummary`` (``tui/src/app/types.rs``).

    Populated server-side by :func:`summarize_counts` and attached to
    :class:`coord.merge_queue.PlannedMerge` so the TUI can render its "2✓ 1✗"
    badges straight from the ``/board`` payload instead of shelling out to
    ``gh pr checks`` itself (#1344).
    """

    passed: int
    failed: int
    running: int
    failed_names: list[str]
    first_failed_url: str | None


@dataclass
class JobStep:
    """One step of a GitHub Actions job (#1892).

    ``conclusion`` mirrors :class:`CheckRun`'s field: ``None`` while the step
    hasn't finished, otherwise success/failure/cancelled/skipped/... — the
    same vocabulary GitHub uses for check-run conclusions, one level down.
    """

    name: str
    conclusion: str | None


@dataclass
class JobRun:
    """One job of a GitHub Actions run — the step-level detail a
    :class:`CheckRun` doesn't carry (#1892).

    Populated only on the CI-failure classification path (see
    :func:`is_verdictless_job`): a :class:`CheckRun` names a *check*
    (workflow name, e.g. ``test (3.12)``), and this is the matching *job*
    (same name, fetched via ``gh api repos/{repo}/actions/runs/{id}/jobs``)
    with its steps. ``runner_name`` is empty when GitHub never assigned this
    job a runner at all — the "cancelled at the queue timeout" signature.
    """

    name: str
    conclusion: str | None
    runner_name: str
    steps: list[JobStep] = field(default_factory=list)


@dataclass
class CheckRun:
    """A single CI check run on a PR.

    ``status`` is the lifecycle phase: queued / in_progress / completed.
    ``conclusion`` is only meaningful when ``status == "completed"`` and is
    normally one of success / failure / cancelled / skipped / neutral /
    timed_out / action_required / stale — but GitHub can and does add new
    conclusions over time, and :class:`coord.ci_github.GitHubCi` synthesizes
    the conclusion ``"unknown"`` when it couldn't read a PR's checks at all
    (#1525). ``failed_checks`` below is an **allow-list**: a completed check
    passes only when its conclusion is affirmatively known-benign
    (``success`` / ``skipped`` / ``neutral``); anything else — including a
    conclusion this module has never seen — blocks the merge gate. ``status
    != "completed"`` is in-flight, handled separately by
    :func:`in_flight_checks`.
    """

    name: str
    status: str
    conclusion: str | None
    url: str
    run_id: str
    started_at: float | None
    completed_at: float | None


@runtime_checkable
class CiStore(Protocol):
    """View of CI checks for a PR, plus (#1851) the one write operation this
    abstraction supports: re-running them.

    ``rerun_for_pr`` is deliberately the *only* mutating method — everything
    else stays read-only exactly as Phase 1 (#240) left it, so every existing
    stub-based test (a plain object/dataclass implementing
    ``list_checks_for_pr``/``is_available`` with no ``rerun_for_pr`` at all)
    keeps passing unmodified: nothing here reads ``rerun_for_pr`` off a
    ``CiStore`` except the #1851 revalidate path, which only ever runs behind
    the ``coord merge --revalidate`` flag.
    """

    def list_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]: ...

    @property
    def is_available(self) -> bool: ...

    def expects_checks(self, repo: str, number: int) -> bool:
        """True when *repo*/*number* should have reported at least one check.

        #1904: ``checks == []`` is genuinely ambiguous — "no CI is
        configured for this repo" (merging is correct) and "CI exists but
        was never triggered" (a throttled webhook, a wedged run, a
        path-filtered-out workflow — merging is wrong) both produce it, and
        every gate predicate (:func:`failed_checks`, :func:`in_flight_checks`,
        :func:`checks_are_stale`) is a filter that reads an empty list as
        "nothing wrong". This is the one method that answers "which of the
        two is this" *without* looking at any particular PR's checks — it
        asks whether the backend believes CI exists for this repo at all.
        Callers (``coord.merge_queue``'s ``checks_absent`` gate) only
        consult this when ``list_checks_for_pr`` has already come back
        empty; a non-empty check list settles the question on its own.

        :class:`NoOpCi` answers ``False`` unconditionally — it is the
        supported "this repo has no CI" opt-out (``ci_store: {type:
        none}``), so nothing it reports should ever read as "checks
        absent". A backend that can't determine this at all should default
        to ``True`` (fail closed, mirroring #1525's "unknown reads as
        blocking" posture) rather than silently reopening the hole this
        method exists to close.
        """
        ...

    def rerun_for_pr(self, repo: str, number: int) -> bool:
        """Re-run *repo*#*number*'s CI workflows. Returns whether it worked.

        Cheap remedy for a CI result staled by base movement (#1851): a CI
        re-run on GitHub-hosted runners costs minutes, not a routed Test-stage
        agent dispatch. Never called unattended — see
        :mod:`coord.revalidate`'s module docstring and
        ``docs/DRIVE_QUEUE.md`` for why ``--revalidate`` is opt-in and
        auto-drain must never trigger work on its own schedule.

        #1892: this same method is ALSO the auto-rerun remedy for a
        verdictless CI failure — see :mod:`coord.merge_queue`'s
        ``_ci_infra_reason``/``MAX_CI_INFRA_RERUNS``. That call site runs
        unattended (unlike ``--revalidate``), which is safe specifically
        *because* the trigger is narrow (every failing check carries no
        verdict about the code) and bounded (capped, then parked for a
        human) — it is not a general license for auto-drain to rerun CI.
        """
        ...

    def list_jobs_for_run(self, repo: str, run_id: str) -> list[JobRun]:
        """Job/step detail for Actions run *run_id* on *repo* (#1892).

        The one piece of data :class:`CheckRun` doesn't carry and the CI
        gate (``failed_checks``/``_PASSING_CONCLUSIONS``) never needed: which
        step (if any) actually ran before a check failed. Exists solely to
        back :func:`is_verdictless_job` — the drive's retry-accounting
        question "did this failure say anything about the code?", never the
        merge gate itself.

        Callers MUST only invoke this after a check has already been found
        failing (:func:`failed_checks` non-empty) — never on the passing or
        pending path, and never from a request-time board read (see
        ``coord.gate_snapshot``'s Invariant 1: the read path performs no
        third-party I/O). Best-effort: a backend that can't answer this
        returns ``[]``, which :func:`is_verdictless_job` always reads as "no
        job data, therefore not verdictless" — the same false-negative bias
        as an unmatched job (see that function's docstring).
        """
        ...


class NoOpCi:
    """Always-available fallback that returns no checks and reruns nothing.

    Used when the user opts out of CI gating with ``ci_store: { type: none }``
    or when no backend is configured.  ``is_available`` is ``False`` so callers
    can distinguish "no CI configured" from "CI says all clear".
    """

    def list_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]:
        return []

    @property
    def is_available(self) -> bool:
        return False

    def expects_checks(self, repo: str, number: int) -> bool:
        """Always ``False`` — CI gating is opted out entirely (#1904), so an
        empty check list is never "checks absent", it's "no CI here"."""
        return False

    def rerun_for_pr(self, repo: str, number: int) -> bool:
        """No-op: CI gating is disabled entirely, so there is nothing to
        re-run and nothing to report as stale (#1851)."""
        return False

    def list_jobs_for_run(self, repo: str, run_id: str) -> list[JobRun]:
        """No-op: CI gating is disabled entirely, so there is no job/step
        detail to fetch (#1892)."""
        return []


# ── Classification helpers ──────────────────────────────────────────────────

# #1525: allow-list of conclusions known to be benign, not a deny-list of
# conclusions known to be bad. Before this, `_FAILED_CONCLUSIONS` enumerated
# {"failure", "cancelled", "timed_out", "action_required"} and anything NOT
# in that set — a `"stale"` conclusion, a future GitHub conclusion this code
# had never seen, or the synthetic `"unknown"` conclusion GitHubCi emits when
# a `gh pr checks` read fails — silently read as "not failing", i.e. passing.
# That is exactly the fail-open shape that let PR #1521 merge over a red
# `test (3.12)` run: an unrecognised or unreadable conclusion must default to
# BLOCKING, never to passing.
_PASSING_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})


def _is_failing_conclusion(conclusion: str | None) -> bool:
    return conclusion not in _PASSING_CONCLUSIONS


def failed_checks(checks: list[CheckRun]) -> list[CheckRun]:
    """Return completed checks whose conclusion is not affirmatively passing.

    Only evaluates ``status == "completed"`` checks — an in-flight check has
    ``conclusion is None`` and is handled by :func:`in_flight_checks`
    instead, not counted as failed here.
    """
    return [
        c for c in checks
        if c.status == "completed" and _is_failing_conclusion(c.conclusion)
    ]


def in_flight_checks(checks: list[CheckRun]) -> list[CheckRun]:
    """Return checks that are queued or running (not yet completed)."""
    return [c for c in checks if c.status != "completed"]


# #1892: two real signatures — recorded from JDonaghy/claude-coordinator run
# 31117792472 and JDonaghy/vimcode run 31119463000, both 2026-08-06 — for a
# CI failure that says nothing about the code:
#
# 1. Never assigned a runner: cancelled at the queue timeout, `runner_name`
#    empty, `steps` empty. GitHub does create a job record for this (unlike
#    a run that never even reaches job-scheduling), but with zero steps.
# 2. Got a runner, died before checkout: exactly one step, named literally
#    "Set up job", with a non-passing conclusion — nothing past it ran, so
#    no repo code executed either.
#
# Deliberately narrow — see this module's `_PASSING_CONCLUSIONS` comment for
# the identical lesson learned the hard way (#1525) about allow-lists vs.
# catch-alls, and the issue's own hazard note: a classifier that is too eager
# becomes a way to launder real failures into "infrastructure". Prefer false
# negatives — a platform failure misread as real costs one manual unblock; a
# real failure misread as platform noise costs a bad merge. So both
# `check is None`/`job is None` (no job data — including a fetch failure;
# see `CiStore.list_jobs_for_run`'s docstring) and any shape that isn't
# EXACTLY one of the two above read as "carries a verdict", never as
# verdictless.
_SET_UP_JOB_STEP_NAME = "Set up job"


def is_verdictless_job(check: CheckRun, job: JobRun | None) -> bool:
    """True when *check* failed for a reason that says nothing about the
    code — see the two signatures documented above (#1892).

    Only meaningful for a check :func:`failed_checks` already selected
    (``status == "completed"`` and a non-passing conclusion); a check that
    is still in flight, or one this function is asked about with no
    matching *job* record, always reads ``False`` — "carries a verdict",
    the safe default per the false-negative bias above.

    This is a **narrower** question than the merge gate's own — it does not
    change whether the check counts as failed (:func:`failed_checks` is
    untouched), only whether the failure is evidence about the *code*. Used
    exclusively by the drive's retry accounting (:mod:`coord.merge_queue`'s
    ``_ci_infra_reason``), never by the gate itself.
    """
    if check.status != "completed" or job is None:
        return False
    if check.conclusion == "cancelled":
        return len(job.steps) == 0
    failed_steps = [
        s for s in job.steps if s.conclusion not in (None, "success", "skipped")
    ]
    return len(failed_steps) == 1 and failed_steps[0].name == _SET_UP_JOB_STEP_NAME


def checks_are_stale(checks: list[CheckRun], base_commit_time: float | None) -> bool:
    """True when a **green** *checks* result predates *base_commit_time* (#1851).

    GitHub attaches ``pull_request`` check runs to the PR's *head* SHA and
    re-runs them on head ``synchronize`` — never on base movement — so a
    check that started before the base's newest commit landed never saw that
    commit. ``started_at`` (not ``completed_at``) is the comparison point:
    what matters is what the base looked like when the run *began*, not when
    it finished.

    Callers should apply :func:`failed_checks`/:func:`in_flight_checks`
    first — this function assumes *checks* is the all-passing remainder and
    doesn't re-derive that itself, so it never contradicts "CI failed"/"CI
    running" with a third, competing reading of the same checks. An empty
    *checks* list (nothing to compare) reads as not-stale; the caller's own
    "no checks" handling covers that case — see :meth:`CiStore.expects_checks`
    and ``coord.merge_queue``'s ``checks_absent`` gate (#1904), which now
    implements exactly that handling at all three call sites.

    Fails closed toward **stale** — mirroring
    :func:`coord.merge_queue._base_move_is_inert`'s documented bias ("a false
    'fresh' merges untested code; a false 'stale' only costs a re-run") —
    whenever the comparison can't be made with confidence: *base_commit_time*
    unreadable/``None``, or any check missing ``started_at``. Clock and
    ordering skew between GitHub's check timestamps and its branch-commit
    timestamps are real; this predicate is not exact, and errs toward the
    cheap re-run rather than the silent stale-green pass.
    """
    if not checks:
        return False
    if base_commit_time is None:
        return True
    return any(c.started_at is None or c.started_at < base_commit_time for c in checks)


def build_ci_store(ci_store_type: str) -> CiStore:
    """Construct the CiStore backend named by ``ci_store_type``.

    Centralised here so callers (merge gate, TUI fetcher, tests) don't need to
    branch on the config value themselves. Unknown types fall back to NoOpCi
    so a typo in coordinator.yml doesn't crash the merge command.
    """
    if ci_store_type == "github":
        from coord.ci_github import GitHubCi  # noqa: PLC0415
        return GitHubCi()
    return NoOpCi()


def summarize(checks: list[CheckRun]) -> str:
    """One-line summary: ``2✓ 1✗`` or ``no checks``.

    Used by the TUI under the Merge stage row and by the CLI when reporting
    why a merge was refused.
    """
    if not checks:
        return "no checks"
    passed = sum(1 for c in checks if c.conclusion == "success")
    failed = len(failed_checks(checks))
    running = len(in_flight_checks(checks))
    parts: list[str] = []
    if passed:
        parts.append(f"{passed}✓")
    if failed:
        parts.append(f"{failed}✗")
    if running:
        parts.append(f"{running}⋯")
    return " ".join(parts) if parts else "no checks"


def summarize_counts(checks: list[CheckRun]) -> CiCheckSummary:
    """Structured rollup of *checks*, mirroring the classification the TUI's
    (now-deleted) ``fetch_ci_check_summary`` used to compute client-side:

    - not yet ``completed`` → running
    - completed + conclusion NOT in ``_PASSING_CONCLUSIONS`` → failed (name +
      first URL captured); this is an allow-list (#1525), so an unrecognised
      or synthetic ``"unknown"`` conclusion counts as failed
    - completed + conclusion in ``_PASSING_CONCLUSIONS`` (success / skipped /
      neutral) → passed

    Used to populate :class:`coord.merge_queue.PlannedMerge.ci_summary` so the
    `/board` payload carries everything the TUI renders as CI badges (#1344).
    """
    # `checks` items are `CheckRun` in production but tests commonly pass
    # lighter duck-typed fakes (see `failed_checks`/`in_flight_checks` above,
    # which only ever touch `.status`/`.conclusion`) — `getattr` with a
    # default keeps this function tolerant of fakes that omit `.url`.
    passed = failed = running = 0
    failed_names: list[str] = []
    first_failed_url: str | None = None
    for c in checks:
        if c.status != "completed":
            running += 1
            continue
        if _is_failing_conclusion(c.conclusion):
            failed += 1
            failed_names.append(c.name)
            url = getattr(c, "url", "") or ""
            if first_failed_url is None and url:
                first_failed_url = url
        else:
            passed += 1
    return CiCheckSummary(
        passed=passed,
        failed=failed,
        running=running,
        failed_names=failed_names,
        first_failed_url=first_failed_url,
    )
