"""#1657: ``coord gates <repo> <issue>`` — read a work row's gate columns
plus the LIVE gate decision (review / test / merge), without a hand-extracted
bearer token and a raw ``/board`` curl.

Two things were missing before this module existed:

1. A CLI surface for the raw columns every gate reads — ``test_state``,
   ``smoke_test``, ``test_reason``, ``test_toolchain`` (#1629),
   ``review_state``, ``review_verdict``, ``review_of_assignment_id`` — none
   of which ``coord status`` or ``coord diagnose --stage test`` prints (see
   #1657's "diagnose --stage test" repro: it reports the *assignment row*'s
   status, never ``test_state`` itself, which was ``"running"`` at the
   moment that mattered).
2. The gate *decision*, not just the columns — in particular whether a
   recorded verdict is #1479-stale (recorded against a base/branch SHA that
   has since moved), which is otherwise unexplainable from any surface the
   operator has: a verdict can read ``passed`` while ``coord merge`` still
   refuses with ``smoke_required``.

:func:`build_gate_report` is the read-only core (board + config + an
optional ``gh_ops`` duck-typed seam in, a :class:`GateReport` out); it
reuses ``coord.merge_queue``'s own review/smoke gate functions
(:func:`~coord.merge_queue.has_approved_review`,
:func:`~coord.merge_queue.evaluate_smoke_verdict`) rather than
re-implementing the #1479 freshness math a second time, so this can never
drift from what ``coord merge``/``coord merge --plan`` actually decide.

Read-only by construction: nothing in this module calls ``save_board``,
``save_queue``, or any ``gh`` write. The synthetic
:class:`~coord.merge_queue.QueuedMerge` built in :func:`build_gate_report`
is never persisted — it exists only to hand the existing gate functions the
duck-typed shape they expect.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from coord.models import WORK_LIKE_TYPES, effective_issue_number

if TYPE_CHECKING:  # avoid import cycles / heavy imports at module load
    from coord.config import Config
    from coord.merge_queue import GhOps
    from coord.models import Assignment, Board

# Mirrors the MergeEvent.kind tokens merge_queue.process() already emits for
# these two refusals — same vocabulary, so grepping the daemon log for
# "smoke_required" finds both a live merge attempt AND a `coord gates` read.
REVIEW_REQUIRED = "review_required"
SMOKE_REQUIRED = "smoke_required"


@dataclass
class AssignmentGateRow:
    """One board row's gate-relevant columns — the raw-dump half of the report."""

    assignment_id: str | None
    type: str
    status: str | None
    branch: str | None
    machine_name: str | None
    provider_name: str | None
    dispatched_at: float | None
    is_interactive: bool | None
    # #1730: the two-number reality #1553 introduced — `issue_number` is what
    # the row is BOOKED to (the tracking/epic issue for an oracle-loop
    # slice), `for_issue_number` is what it's actually FOR (the child), when
    # set. Surfaced on every row so a query that only matched via
    # `effective_issue_number` (see `build_gate_report`) is legible rather
    # than silently showing rows under the "wrong" issue with no explanation.
    issue_number: int
    for_issue_number: int | None
    test_state: str | None
    smoke_test: str | None
    test_reason: str | None
    # #1629 (H-2): the toolchain that produced test_state, when resolvable.
    # None for pre-1629 rows or an unresolvable toolchain — rendered as
    # "unknown", never as a mismatch.
    test_toolchain: str | None
    review_state: str | None
    review_verdict: str | None
    review_of_assignment_id: str | None


@dataclass
class GateDecision:
    """The live decision for one gate (``"review"`` | ``"test"`` | ``"merge"``)."""

    gate: str
    required: bool
    ok: bool
    reason: str | None = None
    # #1479 staleness detail — set only when this gate's refusal is a STALE
    # (not MISSING) verdict.
    anchor: str | None = None  # "base" | "branch"
    recorded_sha: str | None = None
    current_sha: str | None = None


@dataclass
class GateReport:
    repo_name: str
    issue_number: int
    branch: str | None = None
    target_branch: str | None = None
    rows: list[AssignmentGateRow] = field(default_factory=list)
    decisions: list[GateDecision] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _row_from_assignment(a: "Assignment") -> AssignmentGateRow:
    return AssignmentGateRow(
        assignment_id=a.assignment_id,
        type=a.type or "work",
        status=a.status,
        branch=a.branch,
        machine_name=a.machine_name,
        provider_name=a.provider_name,
        dispatched_at=a.dispatched_at,
        is_interactive=None,  # backfilled by _backfill_is_interactive, best-effort
        issue_number=a.issue_number,
        for_issue_number=a.for_issue_number,
        test_state=a.test_state,
        smoke_test=a.smoke_test,
        test_reason=a.test_reason,
        test_toolchain=a.test_toolchain,
        review_state=a.review_state,
        review_verdict=a.review_verdict,
        review_of_assignment_id=a.review_of_assignment_id,
    )


def _backfill_is_interactive(rows: list[AssignmentGateRow]) -> None:
    """Populate ``row.is_interactive`` from the ``assignments`` table.

    #748/#632: ``is_interactive`` is a real DB/wire column deliberately kept
    OFF the ``Assignment`` dataclass (see ``coord.usage.fetch_usage_rows``'s
    docstring) — every board row read via ``Board``/``Assignment`` therefore
    has no way to answer "was this the #555 interactive-review exclusion?"
    without a second, scoped, read-only query. Mutates *rows* in place;
    best-effort — any DB error (e.g. no local DB on a pure thin-client
    process, though ``coord gates`` always runs where the canonical DB
    lives) leaves every ``is_interactive`` at ``None`` rather than raising.
    """
    ids = [r.assignment_id for r in rows if r.assignment_id]
    if not ids:
        return
    try:
        from coord.db import get_connection  # noqa: PLC0415

        conn = get_connection()
        placeholders = ",".join("?" for _ in ids)
        found = conn.execute(
            f"SELECT assignment_id, is_interactive FROM assignments "
            f"WHERE assignment_id IN ({placeholders})",
            ids,
        ).fetchall()
        flags = {r["assignment_id"]: bool(r["is_interactive"]) for r in found}
    except Exception:  # noqa: BLE001 — best-effort enrichment only
        return
    for row in rows:
        if row.assignment_id in flags:
            row.is_interactive = flags[row.assignment_id]


def _select_winning_work_assignment(work_like: list["Assignment"]) -> "Assignment":
    """The work-like row whose branch/verdicts the merge gate actually
    tracks — the most-recently-dispatched one, ties won by the last in
    iteration order. Mirrors
    :func:`coord.merge_queue._select_winning_work_assignment`'s tie-break
    without requiring every row to already be ``status == 'done'`` (unlike
    that helper, this one is used for read-only diagnosis of a row that may
    still be in flight)."""
    winner = work_like[0]
    for a in work_like[1:]:
        if (a.dispatched_at or 0.0) >= (winner.dispatched_at or 0.0):
            winner = a
    return winner


def build_gate_report(
    board: "Board",
    config: "Config",
    repo_name: str,
    issue_number: int,
    gh_ops: "GhOps | None" = None,
) -> GateReport:
    """Read-only: board rows + the live review/test/merge gate decision for
    one ``(repo_name, issue_number)``.

    *gh_ops* (optional, duck-typed like ``coord.merge_queue.GhOps`` —
    normally ``coord.github_ops`` itself) backs the #1479 freshness
    comparison with LIVE branch/base SHAs and the branch's patch-id, mirroring
    exactly what ``coord.merge_queue.process()`` populates on a
    :class:`~coord.merge_queue.QueuedMerge` entry before evaluating its
    gates (see that function's #821/#1475/#1479 comments). Passing ``None``
    skips every live lookup — the decision section then reports only what
    the recorded verdict's own stored anchors already imply (the same
    fail-open convention #821/#1475 established), and the target branch
    resolution falls back to ``repo.default_branch`` without a milestone
    lookup.
    """
    from coord import merge_queue as mq  # noqa: PLC0415

    # #1730: match on the raw `issue_number` (the tracking issue keeps
    # finding its own rows) OR the #1553 *effective* issue — a `for_issue_number`
    # that resolves to *issue_number* means this row's work is FOR the issue
    # being queried even though it's booked to a different (tracking) issue.
    # #1553 taught the TUI this resolution (`Assignment::effective_issue_number`);
    # this CLI had never been updated to match, so `coord gates <repo>
    # <child>` reported "no assignments found" for oracle-loop slices whose
    # only board row carried the tracking issue in `issue_number`.
    matching = [
        a
        for a in (list(board.active) + list(board.completed))
        if a.repo_name == repo_name
        and (a.issue_number == issue_number or effective_issue_number(a) == issue_number)
    ]
    report = GateReport(repo_name=repo_name, issue_number=issue_number)
    if not matching:
        report.notes.append(
            f"no assignments found on the board for {repo_name}#{issue_number}"
        )
        return report

    matching.sort(key=lambda a: a.dispatched_at or 0.0)
    report.rows = [_row_from_assignment(a) for a in matching]
    _backfill_is_interactive(report.rows)

    repo_cfg = config.repo(repo_name) if config is not None else None
    if repo_cfg is None:
        report.notes.append(
            f"repo {repo_name!r} not in coordinator.yml — gate decision unavailable "
            "(the raw columns above are still authoritative)"
        )
        return report

    work_like = [a for a in matching if a.type in WORK_LIKE_TYPES]
    if not work_like:
        report.notes.append(
            "no work-like assignment (work/test-author/mock-author) for this "
            "issue — gate decision unavailable"
        )
        return report

    winner = _select_winning_work_assignment(work_like)
    report.branch = winner.branch
    if not winner.branch:
        report.notes.append(
            f"winning work assignment {winner.assignment_id!r} has no branch — "
            "gate decision unavailable"
        )
        return report

    if gh_ops is not None:
        from coord.branch_model import resolve_base_branch_for_issue_number  # noqa: PLC0415

        target_branch = resolve_base_branch_for_issue_number(
            repo_cfg, repo_cfg.github, issue_number,
        )
    else:
        target_branch = repo_cfg.default_branch
    report.target_branch = target_branch

    # A synthetic QueuedMerge — never persisted (this module never calls
    # save_queue) — duck-typed identically to a real queue entry so it can be
    # handed straight to merge_queue's own gate functions instead of a
    # second, driftable reimplementation of the #1479 freshness math.
    entry = mq.QueuedMerge(
        assignment_id=winner.assignment_id or "",
        repo_name=repo_name,
        repo_github=repo_cfg.github,
        branch=winner.branch,
        target_branch=target_branch,
        issue_number=issue_number,
        issue_title=winner.issue_title or "",
        assignment_type=winner.type or "work",
        required_gates=list(winner.required_gates or []),
    )

    # #821/#1479: populate the freshness anchors LIVE — mirrors exactly what
    # merge_queue.process() does before evaluating the review/smoke gates.
    # This matters because has_approved_review does NOT itself backfill
    # branch_head_sha (only evaluate_smoke_verdict opportunistically
    # backfills base/branch SHAs and patch-id on demand) — without doing it
    # here, a row that never went through a live `coord merge`/auto-drain
    # tick would show every staleness check as a silent no-op.
    if gh_ops is not None:
        try:
            entry.branch_head_sha = gh_ops.get_branch_sha(entry.repo_github, entry.branch)
        except Exception:  # noqa: BLE001 — fail-open: unknown SHA is not blocking
            entry.branch_head_sha = None
        try:
            entry.target_branch_head_sha = gh_ops.get_branch_sha(
                entry.repo_github, entry.target_branch
            )
        except Exception:  # noqa: BLE001
            entry.target_branch_head_sha = None
        try:
            entry.branch_patch_id = gh_ops.get_branch_patch_id(
                entry.repo_github, entry.target_branch, entry.branch
            )
        except Exception:  # noqa: BLE001
            entry.branch_patch_id = None

    review_required = mq.requires_review(entry, config)
    review_ok = True
    review_reason: str | None = None
    if review_required:
        review_ok = mq.has_approved_review(entry, board, gh_ops)
        if not review_ok:
            review_reason = "review required but not approved"
    report.decisions.append(
        GateDecision(gate="review", required=review_required, ok=review_ok, reason=review_reason)
    )

    smoke_required = mq.requires_smoke(entry, config)
    smoke_status = mq.evaluate_smoke_verdict(entry, board, gh_ops) if smoke_required else None
    test_ok = (not smoke_required) or bool(smoke_status and smoke_status.ok)
    test_decision = GateDecision(gate="test", required=smoke_required, ok=test_ok)
    if smoke_status is not None and not smoke_status.ok:
        test_decision.reason = smoke_status.message
        test_decision.anchor = smoke_status.anchor
        test_decision.recorded_sha = smoke_status.recorded_sha
        test_decision.current_sha = smoke_status.current_sha
    report.decisions.append(test_decision)

    merge_blocked_gate: str | None = None
    if review_required and not review_ok:
        merge_blocked_gate = REVIEW_REQUIRED
    elif smoke_required and not test_ok:
        merge_blocked_gate = SMOKE_REQUIRED
    merge_decision = GateDecision(
        gate="merge", required=True, ok=merge_blocked_gate is None, reason=merge_blocked_gate,
    )
    report.decisions.append(merge_decision)
    if merge_decision.ok:
        report.notes.append(
            "merge READY reflects the review/test gates only — CI checks and the "
            "#1318 epic-closing-keyword guard are evaluated live by `coord merge`/"
            "`coord merge --plan`, not by `coord gates`."
        )

    return report


def _short_sha(sha: str | None) -> str:
    return sha[:7] if sha else "unknown"


def format_gate_report(report: GateReport) -> str:
    """Human-readable rendering of *report* for the CLI's default (non-JSON) output."""
    lines: list[str] = [f"gates {report.repo_name}#{report.issue_number}"]

    for row in report.rows:
        lines.append(
            f"  [{row.type}] {row.assignment_id or '?'}  status={row.status}  "
            f"branch={row.branch or '-'}  machine={row.machine_name or '-'}"
            + (f"  provider={row.provider_name}" if row.provider_name else "")
            + (f"  interactive={row.is_interactive}" if row.is_interactive is not None else "")
            # #1730: legible two-number reality — only printed when the row's
            # attribution differs from the issue it's booked to, so an
            # ordinary row (no `for_issue_number`, or one equal to
            # `issue_number`) renders exactly as it did before this field
            # existed.
            + (
                f"  booked_to=#{row.issue_number} for=#{row.for_issue_number}"
                if row.for_issue_number is not None and row.for_issue_number != row.issue_number
                else ""
            )
        )
        lines.append(
            f"      test_state={row.test_state}  smoke_test={row.smoke_test}  "
            f"test_reason={row.test_reason!r}"
        )
        lines.append(
            f"      test_toolchain={row.test_toolchain or 'unknown'}"
        )
        lines.append(
            f"      review_state={row.review_state}  review_verdict={row.review_verdict}  "
            f"review_of_assignment_id={row.review_of_assignment_id or '-'}"
        )

    if report.decisions:
        by_gate = {d.gate: d for d in report.decisions}
        lines.append("")
        lines.append(
            f"Gate decision (branch {report.branch or '?'} -> {report.target_branch or '?'}):"
        )
        review = by_gate.get("review")
        if review is not None:
            if not review.required:
                lines.append("  review : not required")
            elif review.ok:
                lines.append("  review : approve")
            else:
                lines.append(f"  review : BLOCKED — {review.reason}")
        test = by_gate.get("test")
        if test is not None:
            if not test.required:
                lines.append("  test   : not required")
            elif test.ok:
                lines.append("  test   : passed")
            elif test.anchor:
                noun = "base" if test.anchor == "base" else "branch"
                lines.append(
                    f"  test   : STALE — recorded against {noun} "
                    f"{_short_sha(test.recorded_sha)}, {noun} now "
                    f"{_short_sha(test.current_sha)} (#1479)"
                )
                lines.append(f"           {test.reason}")
            else:
                lines.append(f"  test   : BLOCKED — {test.reason}")
        merge = by_gate.get("merge")
        if merge is not None:
            lines.append(
                "  merge  : READY" if merge.ok else f"  merge  : BLOCKED — {merge.reason}"
            )

    for note in report.notes:
        lines.append(f"  note: {note}")

    return "\n".join(lines)


def report_to_dict(report: GateReport) -> dict:
    """JSON-safe ``dict`` for the CLI's ``--json`` flag / the ``/gates``
    daemon response — plain nested dicts/lists, no dataclass instances."""
    return asdict(report)
