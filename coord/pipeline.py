"""Pipeline stage tracking for the assignment lifecycle.

Every "work" assignment passes through a series of approval gates before it
is considered fully shipped.  This module computes a ``PipelineView`` that
describes exactly where an assignment sits in the pipeline so the dashboard
(and any other consumer) can show status and offer one-click gate actions.

The pipeline is intentionally pure-computation: ``compute_pipeline`` takes
already-loaded data structures and returns a value object — no I/O, no side
effects.  The dashboard server wires the real persistence layer.

Pipeline stages (in order):
    coding  → review  → smoke  → merge

Each stage may be "waiting", "active", "completed", or "skipped".  The
``required_gates`` field on the assignment (defaulting to
``config.pipeline.default_gates``) controls which intermediate stages are
enforced — stages not in required_gates are marked "skipped" in the view.

``current_stage`` is a fine-grained state name (e.g. "review_running",
"smoke_passed") that the UI uses for colour-coding and gate routing beyond
what the coarse PipelineStage status captures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coord.config import Config
    from coord.merge_queue import QueuedMerge
    from coord.models import Assignment, Board


# ── Data structures ─────────────────────────────────────────────────────────


@dataclass
class PipelineStage:
    name: str
    status: str   # "active" | "completed" | "skipped" | "waiting"
    is_current: bool = False


@dataclass
class PipelineGate:
    action: str   # e.g. "dispatch_review", "enqueue", "merge", "retry"
    label: str    # button text shown in the dashboard
    endpoint: str  # API path to POST to


@dataclass
class PipelineView:
    assignment_id: str
    issue_number: int
    repo_name: str
    stages: list[PipelineStage]
    current_stage: str
    available_gates: list[PipelineGate]
    progress_pct: int   # 0-100
    # True when the review assignment completed but its findings have not yet
    # been posted to GitHub (review_posted_at is None on the review assignment).
    # The dashboard shows a ⚠ indicator and a "Post Findings" retry button.
    review_findings_pending: bool = False
    # Cached review verdict from the linked review assignment
    # ("approve" | "request-changes" | None).  Populated when the reviewer has
    # completed and emitted a structured REVIEW_VERDICT block.
    review_verdict: str | None = None
    # Full text body of the review findings as cached by notify/auto_loop.
    # Populated from the DB review_findings column so the phone detail screen
    # can render them without a slow GitHub re-fetch.  None when no findings
    # have been cached yet.
    review_findings_body: str | None = None


# ── Stage progression constants ──────────────────────────────────────────────

# Maps a detailed current_stage name to the coarse display stage group.
# Used to determine which PipelineStage.is_current should be set.
_STAGE_GROUP: dict[str, str | None] = {
    "coding": "coding",
    "failed": "coding",   # failure occurred in coding step
    "done": None,          # between steps, nothing highlighted
    "review_running": "review",
    "review_done": "review",
    "review_failed": "review",
    "smoke_running": "smoke",
    "smoke_passed": "smoke",
    "smoke_failed": "smoke",
    "merge_ready": "merge",
    "merging": "merge",
    "merged": "merge",
}

# Approximate progress percentage for each detailed stage.
_PROGRESS: dict[str, int] = {
    "coding": 10,
    "failed": 5,
    "done": 20,
    "review_running": 35,
    "review_done": 50,
    "review_failed": 35,
    "smoke_running": 60,
    "smoke_passed": 70,
    "smoke_failed": 60,
    "merge_ready": 80,
    "merging": 90,
    "merged": 100,
}


# ── Core computation ─────────────────────────────────────────────────────────


def compute_pipeline(
    assignment: "Assignment",
    board: "Board",
    merge_queue_items: list,   # list[QueuedMerge]
    config: "Config",
    *,
    review_findings_body: str | None = None,
) -> PipelineView:
    """Return a PipelineView for a type='work' assignment.

    Scans ``board.active``, ``board.completed``, and ``merge_queue_items`` to
    determine downstream state.  Pure computation — no I/O.
    """
    aid = assignment.assignment_id or ""

    # Resolve effective required_gates: assignment field → config default.
    required_gates: list[str] = (
        assignment.required_gates
        if assignment.required_gates
        else list(config.pipeline.default_gates)
    )

    # ── Find linked downstream assignments ──────────────────────────────────
    all_assignments = list(board.active) + list(board.completed)

    review_assignment: Assignment | None = next(
        (
            a for a in all_assignments
            if a.review_of_assignment_id == aid and a.type == "review"
        ),
        None,
    )
    smoke_assignment: Assignment | None = next(
        (
            a for a in all_assignments
            if a.review_of_assignment_id == aid and a.type == "smoke"
        ),
        None,
    )

    # Find merge queue entry for this assignment.
    mq_entry = next(
        (m for m in merge_queue_items if m.assignment_id == aid),
        None,
    )

    # ── Determine current_stage ──────────────────────────────────────────────
    current_stage: str
    if assignment.status == "running":
        current_stage = "coding"
    elif assignment.status == "failed":
        current_stage = "failed"
    elif assignment.status in ("done", "pending"):
        # Evaluate from most advanced to least advanced.
        if mq_entry is not None:
            from coord.merge_queue import MERGED, MERGING

            if mq_entry.state == MERGED:
                current_stage = "merged"
            elif mq_entry.state == MERGING:
                current_stage = "merging"
            else:
                current_stage = "merge_ready"
        elif assignment.smoke_test == "pass":
            current_stage = "smoke_passed"
        elif assignment.smoke_test == "fail":
            current_stage = "smoke_failed"
        elif smoke_assignment is not None and smoke_assignment.status in ("running", "pending"):
            current_stage = "smoke_running"
        elif smoke_assignment is not None and smoke_assignment.status == "failed":
            # Smoke assignment itself failed (infra failure) — not the same as
            # the smoke *test* failing, but still unblocks the work assignment.
            current_stage = "smoke_failed"
        elif smoke_assignment is not None and smoke_assignment.status in ("done",):
            # Smoke assignment completed but smoke_test not yet set — treat as passed.
            current_stage = "smoke_passed"
        elif review_assignment is not None:
            if review_assignment.status in ("running", "pending"):
                current_stage = "review_running"
            elif review_assignment.status == "failed":
                current_stage = "review_failed"
            else:
                current_stage = "review_done"
        else:
            current_stage = "done"
    else:
        current_stage = "done"

    # ── Build stages list ────────────────────────────────────────────────────
    current_group = _STAGE_GROUP.get(current_stage)
    stages: list[PipelineStage] = []

    # Stages that appear after the current group are "waiting"; those before are
    # "completed".  "active" is the current group if still in progress.

    # Define ordering for stage progression.
    stage_order = ["coding", "review", "smoke", "merge"]
    current_group_idx = (
        stage_order.index(current_group) if current_group in stage_order else -1
    )

    for i, stage_name in enumerate(stage_order):
        if stage_name in ("review", "smoke") and stage_name not in required_gates:
            stages.append(PipelineStage(name=stage_name, status="skipped", is_current=False))
            continue
        if stage_name == "merge" and "merge" not in required_gates:
            stages.append(PipelineStage(name=stage_name, status="skipped", is_current=False))
            continue

        is_current = current_group == stage_name

        if i < current_group_idx:
            # This stage is before the current group → completed.
            status = "completed"
        elif i == current_group_idx:
            # This is the current stage.
            if current_stage == "merged":
                status = "completed"  # final state, show as completed
            elif current_stage in ("smoke_passed", "review_done"):
                status = "completed"  # sub-stage "done", ready for next gate
            elif current_stage == "failed":
                status = "active"  # coding failed, still "active" (needs attention)
            else:
                status = "active"
        else:
            # Future stage.
            status = "waiting"

        stages.append(PipelineStage(name=stage_name, status=status, is_current=is_current))

    # ── Compute available gate actions ───────────────────────────────────────
    _EP = "/api/pipeline/action"
    available_gates: list[PipelineGate] = []

    if current_stage == "done":
        # Only offer review/smoke gates if those stages are actually required.
        if "review" in required_gates:
            available_gates.append(PipelineGate("dispatch_review", "Dispatch Review", _EP))
        if "smoke" in required_gates:
            available_gates.append(PipelineGate("dispatch_smoke", "Dispatch Smoke", _EP))
        available_gates.append(PipelineGate("enqueue", "Queue for Merge", _EP))
    elif current_stage == "review_failed":
        available_gates.append(PipelineGate("dispatch_review", "Dispatch Review", _EP))
    elif current_stage == "review_done":
        available_gates.append(PipelineGate("enqueue", "Queue for Merge", _EP))
        if review_assignment is not None and review_assignment.review_posted_at is None:
            available_gates.append(PipelineGate("post_findings", "Post Findings", _EP))
    elif current_stage == "smoke_passed":
        available_gates.append(PipelineGate("enqueue", "Queue for Merge", _EP))
    elif current_stage == "merge_ready":
        available_gates.append(PipelineGate("merge", "Merge", _EP))
    elif current_stage == "smoke_failed":
        available_gates.append(PipelineGate("dispatch_fix", "Dispatch Fix", _EP))
    elif current_stage == "failed":
        available_gates.append(PipelineGate("retry", "Retry", _EP))

    progress_pct = _PROGRESS.get(current_stage, 0)

    # Determine whether review findings need to be posted.
    review_findings_pending = (
        review_assignment is not None
        and review_assignment.status == "done"
        and review_assignment.review_posted_at is None
    )

    # Derive the cached review verdict from the in-memory review assignment
    # (no I/O — review_assignment is already fetched from the board above).
    review_verdict = review_assignment.review_verdict if review_assignment else None

    return PipelineView(
        assignment_id=aid,
        issue_number=assignment.issue_number,
        repo_name=assignment.repo_name,
        stages=stages,
        current_stage=current_stage,
        available_gates=available_gates,
        progress_pct=progress_pct,
        review_findings_pending=review_findings_pending,
        review_verdict=review_verdict,
        review_findings_body=review_findings_body,
    )
