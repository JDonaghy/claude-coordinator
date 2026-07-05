"""Server-side per-issue stage/gate projection (#550, generalizes #776/#778).

``coord-tui``'s ``tui/src/app/pipeline.rs`` independently re-derives every
stage/gate computation from the raw ``/board`` rows — ``stage_status_for``,
``merge_stage_status_for``, ``test_stage_status_for``, and
``issue_has_any_approved_review`` (which duplicates
``coord.merge_queue.has_approved_review``'s intent, but keyed by issue
number rather than branch).  This module computes the same *DB-derivable*
subset of that logic once, in Python, so it can be injected into ``/board``
(``coord/serve_app.py``) and consumed by the TUI instead of re-implemented.

Deliberately excluded — genuinely TUI-session-local state with no server
equivalent, so it stays a client-side overlay on top of this projection:

* the optimistic "merge just dispatched" flag (``pipeline_inflight_merges``)
  set the instant the Go button is pressed, before the DB round-trip lands;
* a locally-spawned Phase-1 build subprocess (``test_build_in_flight``);
* the CI-check cache the TUI itself polls via the GitHub API
  (``pipeline_ci_checks``) — this module uses the server's own ``CiStore``
  instead, which is a *different* (also valid) CI signal source already
  wired into ``coord.merge_queue``'s gate evaluation.

Because local-SQLite-mode coord-tui (no ``coord serve`` daemon configured)
has no server to ask, the Rust functions this mirrors are NOT deleted —
they remain the local-mode fallback. The daemon path prefers this
projection when present. See #550 for the full rationale.

Pure computation: every function here takes already-loaded data and returns
plain values — no I/O, no side effects.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# ── Stage-status vocabulary — mirrors tui/src/app/pipeline.rs::StageStatus ──
PENDING = "pending"
ACTIVE = "active"
DONE = "done"
FAILED = "failed"
STALE = "stale"
SKIPPED = "skipped"

_MERGED_STATES = frozenset({"merged"})
_ACTIVE_MERGE_STATES = frozenset({"open", "queued"})
_FAILED_MERGE_STATES = frozenset({"failed", "human_required"})


@runtime_checkable
class _AssignmentLike(Protocol):
    assignment_id: str | None
    type: str
    status: str
    dispatched_at: float | None
    review_verdict: str | None
    review_of_assignment_id: str | None
    test_state: str | None
    repo_name: str
    issue_number: int
    acceptance_state: str | None
    acceptance_total: int | None
    acceptance_passed: int | None


# ── Helpers ──────────────────────────────────────────────────────────────


def _latest_by_dispatch(assignments: list) -> Any | None:
    """Return the assignment with the max ``dispatched_at`` (None sorts last,
    matching Rust's ``partial_cmp(...).unwrap_or(Equal)`` on an Option)."""
    if not assignments:
        return None
    return max(
        assignments,
        key=lambda a: (a.dispatched_at is not None, a.dispatched_at or 0.0),
    )


def _issue_has_plan_assignment(assignments_for_issue: list) -> bool:
    return any(a.type == "plan" for a in assignments_for_issue)


def assignments_for_stage(
    assignments_for_issue: list,
    stage: str,
    *,
    require_plan: bool,
) -> list:
    """Mirrors ``pipeline.rs::assignments_for_stage``.

    When the pipeline has no Plan stage in this issue's strip (no global
    ``require_plan`` and no ``type="plan"`` assignment for this issue),
    plan-typed assignments fold into "work" so a ``--plan-only`` dispatch
    without ``require_plan`` doesn't disappear from the Work stage.
    """
    fold_plan_into_work = (
        stage == "work"
        and not require_plan
        and not _issue_has_plan_assignment(assignments_for_issue)
    )
    out = []
    for a in assignments_for_issue:
        t = a.type or "work"
        if fold_plan_into_work:
            if t in ("work", "plan"):
                out.append(a)
        elif t == stage:
            out.append(a)
    return out


def upstream_max_dispatched_at(
    assignments_for_issue: list,
    stage: str,
    stage_names: list[str],
    *,
    require_plan: bool,
) -> float | None:
    """Mirrors ``pipeline.rs::upstream_max_dispatched_at``."""
    if stage not in stage_names:
        return None
    idx = stage_names.index(stage)
    if idx == 0:
        return None
    best: float | None = None
    for s in stage_names[:idx]:
        for a in assignments_for_stage(assignments_for_issue, s, require_plan=require_plan):
            if a.dispatched_at is not None:
                best = a.dispatched_at if best is None else max(best, a.dispatched_at)
    return best


def _has_active_conflict_fix(assignments_for_issue: list) -> bool:
    """Mirrors ``pipeline.rs::has_active_conflict_fix`` (#241)."""
    return any(
        a.type == "conflict-fix" and a.status in ("running", "pending")
        for a in assignments_for_issue
    )


def _has_active_smoke_session(assignments_for_issue: list) -> bool:
    """Mirrors ``pipeline.rs::has_active_smoke_session`` (#585)."""
    return any(
        a.type in ("smoke", "test-chat") and a.status in ("running", "pending")
        for a in assignments_for_issue
    )


def _ci_failed_for_entry(merge_entry: Any | None, ci_store: Any | None) -> bool:
    """Mirrors ``pipeline.rs::ci_failed_for_entry``, sourced from the
    server's ``CiStore`` rather than the TUI's own poll cache."""
    if ci_store is None or not getattr(ci_store, "is_available", False):
        return False
    if merge_entry is None or getattr(merge_entry, "pr_number", None) is None:
        return False
    from coord.ci_store import failed_checks  # noqa: PLC0415

    checks = ci_store.list_checks_for_pr(merge_entry.repo_github, merge_entry.pr_number)
    return bool(failed_checks(checks))


# ── Per-stage status functions ──────────────────────────────────────────


def stage_status_for_internal_work(
    assignments_for_issue: list,
    *,
    is_closed: bool,
    require_plan: bool,
) -> str:
    """Mirrors ``pipeline.rs::stage_status_for_internal_work``."""
    matching = assignments_for_stage(assignments_for_issue, "work", require_plan=require_plan)
    if any(a.status == "running" for a in matching):
        return ACTIVE
    latest = _latest_by_dispatch(matching)
    if latest is not None:
        if latest.status == "done":
            return DONE
        if latest.status == "failed":
            return FAILED
    return SKIPPED if is_closed else PENDING


def test_stage_status_for(
    assignments_for_issue: list,
    *,
    is_closed: bool,
    require_plan: bool,
) -> str:
    """Mirrors ``pipeline.rs::test_stage_status_for`` (#200/#235/#310/#585).

    Excludes the #235 "Phase 1 build in flight" override — that's a locally
    spawned TUI subprocess with no server-side equivalent; the TUI overlays
    it on top of this value.
    """
    work_status = stage_status_for_internal_work(
        assignments_for_issue, is_closed=is_closed, require_plan=require_plan
    )
    if work_status != DONE:
        return SKIPPED if is_closed else PENDING

    if _has_active_smoke_session(assignments_for_issue):
        return ACTIVE

    work = assignments_for_stage(assignments_for_issue, "work", require_plan=require_plan)
    with_verdict = [a for a in work if (a.test_state or "") != ""]
    verdict_assignment = _latest_by_dispatch(with_verdict)
    verdict = verdict_assignment.test_state if verdict_assignment else None
    if verdict in ("passed", "skipped"):
        return DONE
    if verdict == "failed":
        return FAILED
    return PENDING


def acceptance_stage_status_for(assignments_for_issue: list) -> str:
    """The Acceptance box's status (#932/#944) — reported and gated
    *separately* from the Test box (docs/ORACLE_LOOP.md), so this mirrors
    ``test_stage_status_for``'s shape but keys off
    ``Assignment.acceptance_state`` rather than a dedicated assignment
    type: ``coord acceptance record`` stamps the verdict directly onto the
    work assignment row (see ``coord/commands/acceptance.py``), it never
    spawns a separate ``type="acceptance"`` assignment.

    Distinct from Test in one more way: an issue with no acceptance suite
    authored yet (no manifest slice, so ``acceptance record`` was never run
    against it) has no signal at all — SKIPPED rather than PENDING, since
    the Acceptance box only applies to oracle-loop milestones, not every
    issue on the board.
    """
    work = [a for a in assignments_for_issue if (a.type or "work") == "work"]
    with_state = [a for a in work if (a.acceptance_state or "") != ""]
    if not with_state:
        return SKIPPED
    latest = _latest_by_dispatch(with_state)
    state = latest.acceptance_state
    if state == "passed":
        return DONE
    if state == "failed":
        return FAILED
    return PENDING


def acceptance_progress_for(assignments_for_issue: list) -> dict[str, int] | None:
    """``{"passed": p, "total": t}`` from the latest recorded acceptance
    verdict for this issue, or ``None`` when no verdict exists yet or it
    predates #932's per-test counts. Backs the Acceptance box's
    partial-green display (e.g. "3/7 acceptance green") — a growing suite
    is *expected* to read sub-100% until the feature completes
    (docs/ORACLE_LOOP.md), so this is reporting, not a pass/fail gate.
    """
    work = [a for a in assignments_for_issue if (a.type or "work") == "work"]
    with_state = [a for a in work if (a.acceptance_state or "") != ""]
    if not with_state:
        return None
    latest = _latest_by_dispatch(with_state)
    if latest.acceptance_total is None or latest.acceptance_passed is None:
        return None
    return {"passed": latest.acceptance_passed, "total": latest.acceptance_total}


def merge_stage_status_for(
    assignments_for_issue: list,
    merge_entry: Any | None,
    *,
    is_closed: bool,
    ci_store: Any | None = None,
) -> str:
    """Mirrors ``pipeline.rs::merge_stage_status_for`` (#241/#290/#775).

    Excludes the #290 "just dispatched, DB not yet caught up" optimistic
    flag — that's TUI-session-local; the TUI overlays it on top.
    """
    if _has_active_conflict_fix(assignments_for_issue):
        return ACTIVE

    if merge_entry is not None:
        state = merge_entry.state
        if state in _MERGED_STATES:
            return DONE
        if state in _ACTIVE_MERGE_STATES:
            return ACTIVE
        if state in _FAILED_MERGE_STATES:
            return FAILED

    if _ci_failed_for_entry(merge_entry, ci_store):
        return FAILED

    # #775: the daemon's merge-reconcile tick prunes the queue row after
    # flipping the work assignment to status="merged" — fall back to the
    # assignment itself as evidence the Merge stage is Done.
    if any(a.type == "work" and a.status == "merged" for a in assignments_for_issue):
        return DONE

    return SKIPPED if is_closed else PENDING


def stage_status_for(
    assignments_for_issue: list,
    stage: str,
    *,
    stage_names: list[str],
    is_closed: bool,
    require_plan: bool,
    merge_entry: Any | None = None,
    ci_store: Any | None = None,
) -> str:
    """Mirrors ``pipeline.rs::stage_status_for`` — the generic per-stage
    dispatcher, including the #193 "stale downstream verdict" check.
    """
    if stage == "merge":
        return merge_stage_status_for(
            assignments_for_issue, merge_entry, is_closed=is_closed, ci_store=ci_store
        )
    if stage == "test":
        return test_stage_status_for(
            assignments_for_issue, is_closed=is_closed, require_plan=require_plan
        )

    matching = assignments_for_stage(assignments_for_issue, stage, require_plan=require_plan)
    if any(a.status == "running" for a in matching):
        return ACTIVE

    latest = _latest_by_dispatch(matching)
    if latest is not None:
        mapped: str | None = None
        if latest.status == "done" and stage == "review":
            # #473/#812: key off the verdict, not merely that review ran.
            mapped = DONE if latest.review_verdict == "approve" else FAILED
        elif latest.status == "done":
            mapped = DONE
        elif latest.status == "failed":
            mapped = FAILED

        if mapped is not None:
            if latest.dispatched_at is not None:
                upstream = upstream_max_dispatched_at(
                    assignments_for_issue, stage, stage_names, require_plan=require_plan
                )
                if upstream is not None and upstream > latest.dispatched_at:
                    return STALE
            return mapped

    return SKIPPED if is_closed else PENDING


def issue_has_any_approved_review(
    assignments_for_issue: list,
    seed_work_id: str | None = None,
) -> bool:
    """Mirrors ``pipeline.rs::issue_has_any_approved_review`` (#292/#331).

    Also the issue-scoped equivalent of
    ``coord.merge_queue.has_approved_review`` (which is branch-scoped, keyed
    off a single ``QueuedMerge`` entry) — this collects every work
    assignment for the *issue* so a bounce-created fix worker's approval is
    found even when a merge-queue entry is still keyed to the original work.
    """
    work_ids = {a.assignment_id for a in assignments_for_issue if a.type == "work" and a.assignment_id}
    if seed_work_id:
        work_ids.add(seed_work_id)

    # #331: self-approval / PR-comment-fallback verdict stamped directly on
    # a work assignment.
    if any(
        a.assignment_id in work_ids and a.review_verdict == "approve"
        for a in assignments_for_issue
    ):
        return True

    if not work_ids:
        return False

    return any(
        a.type == "review"
        and a.review_of_assignment_id in work_ids
        and a.review_verdict == "approve"
        for a in assignments_for_issue
    )


# ── Board-level projection ──────────────────────────────────────────────


def pipeline_stage_names(default_gates: list[str]) -> list[str]:
    """Mirrors ``pipeline.rs::pipeline_stage_names`` (module-default, no
    per-issue plan-assignment prepend — see ``issue_stage_names``)."""
    stages = ["work"]
    for g in default_gates:
        # #738: "merge" is retired from the per-issue pipeline strip; it's
        # computed here regardless (for the Kanban badge / Merge Queue panel)
        # but excluded from the per-issue stage-name ordering.
        if g not in ("work", "plan", "merge"):
            stages.append(g)
    return stages


def issue_stage_names(assignments_for_issue: list, default_gates: list[str]) -> list[str]:
    """Mirrors ``pipeline.rs::pipeline_stage_names_for_issue``."""
    stages = pipeline_stage_names(default_gates)
    if stages[0] != "plan" and _issue_has_plan_assignment(assignments_for_issue):
        stages = ["plan", *stages]
    return stages


def compute_issue_projection(
    assignments_for_issue: list,
    merge_entry: Any | None,
    *,
    is_closed: bool,
    require_plan: bool,
    default_gates: list[str],
    ci_store: Any | None = None,
) -> dict[str, Any]:
    """Compute the full per-issue stage badge dict.

    ``stages`` covers every name in this issue's stage strip (``plan``?,
    ``work``, then the configured gates minus ``work``/``plan``/``merge``)
    plus ``merge`` itself (computed unconditionally — the Kanban badge and
    Merge Queue panel need it even though it's excluded from the per-issue
    stage strip proper, #738).
    """
    names = issue_stage_names(assignments_for_issue, default_gates)
    stages: dict[str, str] = {}
    for name in names:
        stages[name] = stage_status_for(
            assignments_for_issue,
            name,
            stage_names=names,
            is_closed=is_closed,
            require_plan=require_plan,
            merge_entry=merge_entry,
            ci_store=ci_store,
        )
    stages["merge"] = merge_stage_status_for(
        assignments_for_issue, merge_entry, is_closed=is_closed, ci_store=ci_store
    )
    # #932: the Acceptance box, computed unconditionally like "merge" above
    # (own box, own verdict — reported separately from the Test stage) and
    # excluded from the per-issue stage-strip ordering that `default_gates`
    # drives, since it only applies to oracle-loop milestones.
    stages["acceptance"] = acceptance_stage_status_for(assignments_for_issue)
    return {
        "stages": stages,
        "acceptance_progress": acceptance_progress_for(assignments_for_issue),
        "has_approved_review": issue_has_any_approved_review(
            assignments_for_issue,
            seed_work_id=merge_entry.assignment_id if merge_entry is not None else None,
        ),
    }


def compute_board_stage_projection(
    *,
    issues: list[dict],
    assignments: list,
    merge_queue_items: list,
    default_gates: list[str],
    require_plan: bool = False,
    ci_store: Any | None = None,
) -> list[dict[str, Any]]:
    """Compute the per-issue stage projection for every issue that appears
    on the board — the payload injected into ``GET /board`` as
    ``issue_stage_projection``.

    Issue keys are ``(repo_name, issue_number)`` — the union of the
    ``issues`` table (open + recently-synced) and every assignment's
    ``(repo_name, issue_number)`` (so closed issues with assignment history
    still get a projection, matching what the TUI's Pipeline tab shows).
    """
    is_closed_by_key: dict[tuple[str, int], bool] = {
        (i["repo_name"], i["number"]): str(i.get("state", "")).lower() == "closed"
        for i in issues
    }
    issue_title_by_key: dict[tuple[str, int], str] = {
        (i["repo_name"], i["number"]): i.get("title", "") for i in issues
    }

    assignments_by_key: dict[tuple[str, int], list] = {}
    for a in assignments:
        if not a.repo_name or a.issue_number is None:
            continue
        assignments_by_key.setdefault((a.repo_name, a.issue_number), []).append(a)

    merge_by_key: dict[tuple[str, int], Any] = {}
    for m in merge_queue_items:
        key = (m.repo_name, m.issue_number)
        # First-match-wins, mirroring `.find()` over the id-ordered list —
        # `load_queue()`/`board_projection()` both order by `id` ascending.
        merge_by_key.setdefault(key, m)

    keys = set(is_closed_by_key) | set(assignments_by_key)

    result: list[dict[str, Any]] = []
    for repo_name, issue_number in keys:
        key = (repo_name, issue_number)
        issue_assignments = assignments_by_key.get(key, [])
        entry = compute_issue_projection(
            issue_assignments,
            merge_by_key.get(key),
            is_closed=is_closed_by_key.get(key, False),
            require_plan=require_plan,
            default_gates=default_gates,
            ci_store=ci_store,
        )
        entry["repo_name"] = repo_name
        entry["issue_number"] = issue_number
        entry["issue_title"] = issue_title_by_key.get(key, "")
        result.append(entry)

    result.sort(key=lambda e: (e["repo_name"], e["issue_number"]))
    return result
