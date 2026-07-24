"""Format and parse coordinator-authored issue comments.

Comments are the message bus between the coordinator and human reviewers. They
need to be readable in GitHub's UI *and* parseable by future automation, so
each comment carries an HTML-comment marker with key=value metadata.

Marker grammar: `<!-- coord:event=<event> assignment=<id> ... -->`
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable

EVENT_BRIEFING = "briefing"
EVENT_COMPLETION = "completion"
EVENT_FAILURE = "failure"
EVENT_STUCK = "stuck"
EVENT_PLAN = "plan"
EVENT_ADVISORY = "advisory"
# #846: an assignment running past its wall-clock threshold, or thrashing
# through fix/review rounds without converging. Distinct from EVENT_STUCK
# (a worker self-reported STATUS/STUCK line) — this fires from time/round
# based detection, which also catches the "silently burned budget, looked
# productive the whole time" failure mode STUCK: lines miss (see #448).
EVENT_NEEDS_ATTENTION = "needs_attention"


@dataclass
class CommentMarker:
    event: str
    fields: dict[str, str]


_MARKER_RE = re.compile(r"<!--\s*coord:(?P<body>[^>]*?)\s*-->")
_FIELD_RE = re.compile(r"(\w+)=(\S+)")

# ── Review-findings block ────────────────────────────────────────────────────
# The FULL review body, embedded in a completion comment under a parseable
# marker so a fix worker can recover it from the GitHub message bus on ANY
# machine (no shared DB required) — keyed by the review assignment id.
FINDINGS_BEGIN = "coord:review-findings"
FINDINGS_END = "coord:review-findings-end"


def format_findings_block(assignment_id: str, verdict: str | None, body: str) -> str:
    """Render the full review findings as a marked, GitHub-readable block."""
    v = f" verdict={verdict}" if verdict else ""
    return (
        f"<!-- {FINDINGS_BEGIN} assignment={assignment_id}{v} -->\n"
        f"### Review findings\n\n{body.strip()}\n"
        f"<!-- {FINDINGS_END} -->"
    )


def extract_findings_block(
    comment_body: str, assignment_id: str
) -> tuple[str | None, str] | None:
    """Return ``(verdict, body)`` from a comment carrying the marked findings
    block for ``assignment_id``, or ``None`` when absent.  ``verdict`` is read
    from the marker and may be ``None`` if it wasn't recorded."""
    pat = re.compile(
        r"<!--\s*" + re.escape(FINDINGS_BEGIN) + r"\s+assignment="
        + re.escape(assignment_id) + r"(?P<attrs>[^>]*)-->\s*(?P<body>.*?)\s*<!--\s*"
        + re.escape(FINDINGS_END) + r"\s*-->",
        re.DOTALL,
    )
    m = pat.search(comment_body or "")
    if not m:
        return None
    vm = re.search(r"verdict=(\S+)", m.group("attrs") or "")
    verdict = vm.group(1) if vm else None
    body = re.sub(r"^#+\s*Review findings\s*\n+", "", m.group("body").strip(), count=1)
    body = body.strip()
    if not body:
        return None
    return (verdict, body)


# ── Milestone Outcome Audit scorecard (#886 Phase 2) ────────────────────────
# The structured, versioned verdict from a `--audit-of` run (#885), embedded
# under a parseable marker so ANY machine can recover the full goal-by-goal
# JSON from the GitHub message bus alone — the same "no shared DB required"
# design as the review-findings block above.
AUDIT_BEGIN = "coord:audit-scorecard"
AUDIT_END = "coord:audit-scorecard-end"


def format_audit_scorecard(
    *,
    assignment_id: str,
    run_number: int,
    bottom_line: str,
    goals: list[dict],
    diff: dict[str, list[str]] | None = None,
) -> str:
    """Render a milestone-audit scorecard: a human-readable table + delta vs
    the prior run, with the raw goal JSON tucked in a collapsible section so
    a future agent (or `extract_audit_scorecard`) can recover it exactly."""

    def _cell(v: object) -> str:
        return str(v if v is not None else "").replace("|", "\\|").replace("\n", " ")

    table = ["| Goal | Before | After | Verdict | Evidence |", "|---|---|---|---|---|"]
    for g in goals:
        table.append(
            f"| {_cell(g.get('goal'))} | {_cell(g.get('metric_before'))} | "
            f"{_cell(g.get('metric_after'))} | {_cell(g.get('verdict'))} | "
            f"{_cell(g.get('evidence'))} |"
        )
    lines = [
        f"### Milestone outcome audit — run v{run_number}",
        f"<!-- {AUDIT_BEGIN} assignment={assignment_id} run={run_number} -->",
        "",
        f"**Bottom line:** {bottom_line or '(none provided)'}",
        "",
        *table,
    ]
    if diff and any(diff.get(k) for k in ("closed", "regressed", "still_open", "new")):
        lines.append("")
        lines.append(f"**Delta vs run v{run_number - 1}:**")
        if diff.get("closed"):
            lines.append(f"- ✅ closed: {', '.join(diff['closed'])}")
        if diff.get("regressed"):
            lines.append(f"- ⚠️ regressed: {', '.join(diff['regressed'])}")
        if diff.get("still_open"):
            lines.append(f"- still open: {', '.join(diff['still_open'])}")
        if diff.get("new"):
            lines.append(f"- new goals: {', '.join(diff['new'])}")
    lines.append("")
    lines.append("<details><summary>Structured verdict (JSON)</summary>")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(goals, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("</details>")
    lines.append(f"<!-- {AUDIT_END} -->")
    return "\n".join(lines)


def extract_audit_scorecard(comment_body: str) -> dict | None:
    """Parse a marked audit scorecard back into
    ``{"assignment_id", "run_number", "bottom_line", "goals"}``, or ``None``
    if the comment carries no scorecard block. Round-trip counterpart to
    :func:`format_audit_scorecard`."""
    pat = re.compile(
        r"<!--\s*" + re.escape(AUDIT_BEGIN)
        + r"\s+assignment=(?P<aid>\S+)\s+run=(?P<run>\d+)\s*-->"
        r"(?P<body>.*?)<!--\s*" + re.escape(AUDIT_END) + r"\s*-->",
        re.DOTALL,
    )
    m = pat.search(comment_body or "")
    if not m:
        return None
    body = m.group("body")
    bl_m = re.search(r"\*\*Bottom line:\*\*\s*(.+)", body)
    bottom_line = bl_m.group(1).strip() if bl_m else ""
    json_m = re.search(r"```json\s*(\[.*?\])\s*```", body, re.DOTALL)
    try:
        goals = json.loads(json_m.group(1)) if json_m else []
    except (TypeError, ValueError):
        goals = []
    return {
        "assignment_id": m.group("aid"),
        "run_number": int(m.group("run")),
        "bottom_line": bottom_line,
        "goals": goals,
    }


def _marker(event: str, **fields: str | int | None) -> str:
    parts = [f"event={event}"]
    for k, v in fields.items():
        if v is None or v == "":
            continue
        parts.append(f"{k}={v}")
    return f"<!-- coord:{' '.join(parts)} -->"


def parse_marker(body: str) -> CommentMarker | None:
    """Return the first coord marker in a comment body, or None."""
    m = _MARKER_RE.search(body)
    if not m:
        return None
    raw = m.group("body")
    fields = dict(_FIELD_RE.findall(raw))
    event = fields.pop("event", "")
    if not event:
        return None
    return CommentMarker(event=event, fields=fields)


# ── Unified parse for durable storage (#873) ─────────────────────────────────
# coord.review posts a differently-shaped header — `<!-- coord:review
# verdict=... -->` (no `event=` token) — which parse_marker() above doesn't
# recognise (it requires `event=`). A self-contained regex is duplicated here
# rather than importing coord.review, to avoid a circular import: github_ops
# (the #873 capture-at-write choke point) needs coord.state, and coord.review
# already imports coord.github_ops.
_REVIEW_MARKER_RE = re.compile(r"<!--\s*coord:review\s+([^>]+?)\s*-->")


def parse_coord_comment_marker(body: str) -> dict[str, str | None] | None:
    """Unified marker parse across all coord comment grammars, for the
    ``issue_comments`` durable mirror (#873).

    Tries the generic ``coord:event=...`` marker first (covers
    briefing/completion/failure/stuck/plan/advisory/needs_attention), then
    falls back to the ``coord:review verdict=...`` header format used by
    posted review bodies. Returns ``{"event", "assignment_id", "machine",
    "verdict"}`` (any value may be ``None``), or ``None`` when *body* carries
    no recognisable coord marker at all.
    """
    marker = parse_marker(body)
    if marker is not None:
        return {
            "event": marker.event,
            "assignment_id": marker.fields.get("assignment"),
            "machine": marker.fields.get("machine"),
            "verdict": marker.fields.get("verdict"),
        }
    m = _REVIEW_MARKER_RE.search(body or "")
    if not m:
        return None
    fields = dict(_FIELD_RE.findall(m.group(1)))
    if "verdict" not in fields:
        return None
    return {
        "event": "review",
        "assignment_id": fields.get("assignment"),
        "machine": fields.get("reviewer"),
        "verdict": fields.get("verdict"),
    }


def _fmt_files(files: Iterable[str]) -> str:
    files = list(files)
    if not files:
        return "(unspecified)"
    return ", ".join(f"`{f}`" for f in files)


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def format_briefing(
    *,
    assignment_id: str,
    machine_name: str,
    repo_name: str,
    issue_number: int,
    briefing: str,
    files_likely: Iterable[str] = (),
    do_not_touch: Iterable[tuple[str, str]] = (),
) -> str:
    """Build the issue comment posted when an assignment is dispatched.

    `do_not_touch` is a sequence of (file_path, reason) pairs describing files
    other workers are currently touching.
    """
    marker = _marker(
        EVENT_BRIEFING,
        assignment=assignment_id,
        machine=machine_name,
        repo=repo_name,
        issue=issue_number,
    )
    lines = [
        "## Coordinator Assignment",
        marker,
        f"**Machine:** {machine_name}",
        f"**Repo:** {repo_name}",
        f"**Files:** {_fmt_files(files_likely)}",
    ]
    dnt = list(do_not_touch)
    if dnt:
        rendered = ", ".join(f"`{path}` ({reason})" for path, reason in dnt)
        lines.append(f"**Do not touch:** {rendered}")
    else:
        lines.append("**Do not touch:** (nothing else in flight)")
    lines.append("")
    lines.append("### Briefing")
    lines.append(briefing.strip() or "(no briefing provided)")
    return "\n".join(lines)


def format_completion(
    *,
    assignment_id: str,
    machine_name: str,
    repo_name: str,
    issue_number: int,
    exit_code: int,
    duration_seconds: float | None = None,
    log_path: str | None = None,
    summary: str = "",
) -> str:
    marker = _marker(
        EVENT_COMPLETION,
        assignment=assignment_id,
        machine=machine_name,
        repo=repo_name,
        issue=issue_number,
        exit_code=exit_code,
    )
    lines = [
        "## Coordinator: Assignment Complete",
        marker,
        f"**Machine:** {machine_name}",
        f"**Status:** done",
        f"**Exit code:** {exit_code}",
        f"**Duration:** {_fmt_duration(duration_seconds)}",
    ]
    if log_path:
        lines.append(f"**Log:** `{log_path}`")
    if summary.strip():
        lines.append("")
        lines.append("### Summary")
        lines.append(summary.strip())
    return "\n".join(lines)


def format_stuck(
    *,
    assignment_id: str,
    machine_name: str,
    repo_name: str,
    issue_number: int,
    stuck_message: str,
) -> str:
    marker = _marker(
        EVENT_STUCK,
        assignment=assignment_id,
        machine=machine_name,
        repo=repo_name,
    )
    lines = [
        marker,
        f"## ⚠️ Worker STUCK",
        f"**Machine:** {machine_name}",
        f"**Assignment:** {assignment_id}",
        f"**Issue:** #{issue_number}",
        "",
        stuck_message.strip(),
        "",
        "The worker has stopped and is waiting for guidance. Use:",
        f"`coord resume-stuck {assignment_id} --guidance \"your answer here\"`",
    ]
    return "\n".join(lines)


def format_needs_attention(
    *,
    assignment_id: str,
    machine_name: str,
    repo_name: str,
    issue_number: int,
    reason: str,
    detail: str,
) -> str:
    """Format a "needs attention" comment (#846) — a time/round-based signal
    that an assignment is running long or thrashing, distinct from a
    worker's self-reported ``STUCK:`` line.

    *reason* is ``"wall_clock"`` or ``"non_convergence"``; *detail* is a
    human-readable one-liner (e.g. "running 52m, threshold 45m" or
    "4 fix/review rounds without a green test + approved review").
    """
    marker = _marker(
        EVENT_NEEDS_ATTENTION,
        assignment=assignment_id,
        machine=machine_name,
        repo=repo_name,
        reason=reason,
    )
    lines = [
        marker,
        "## ⏱ Needs attention",
        f"**Machine:** {machine_name}",
        f"**Assignment:** {assignment_id}",
        f"**Issue:** #{issue_number}",
        f"**Reason:** {'Running too long' if reason == 'wall_clock' else 'Not converging'}",
        "",
        detail.strip(),
        "",
        "Detection + surfacing only — nothing was killed or reassigned. A "
        "human should take a look; use `coord log <id>` to see what it's "
        "doing, or `coord retry`/`coord stop` to intervene.",
    ]
    return "\n".join(lines)


def format_failure(
    *,
    assignment_id: str,
    machine_name: str,
    repo_name: str,
    issue_number: int,
    exit_code: int | None,
    duration_seconds: float | None = None,
    log_path: str | None = None,
    error: str = "",
) -> str:
    marker = _marker(
        EVENT_FAILURE,
        assignment=assignment_id,
        machine=machine_name,
        repo=repo_name,
        issue=issue_number,
        exit_code=exit_code if exit_code is not None else "",
    )
    lines = [
        "## Coordinator: Assignment Failed",
        marker,
        f"**Machine:** {machine_name}",
        f"**Status:** failed",
        f"**Exit code:** {exit_code if exit_code is not None else '—'}",
        f"**Duration:** {_fmt_duration(duration_seconds)}",
    ]
    if log_path:
        lines.append(f"**Log:** `{log_path}`")
    if error.strip():
        lines.append("")
        lines.append("### Error")
        lines.append(error.strip())
    return "\n".join(lines)


def format_advisory(
    *,
    assignment_id: str,
    machine_name: str,
    repo_name: str,
    issue_number: int,
    duration_seconds: float | None = None,
    log_path: str | None = None,
    reason: str = "",
) -> str:
    """Format an advisory comment for a 0-commit clean exit.

    Distinct from both completion (no code was produced) and failure (the
    worker exited cleanly — exit_code 0 — so no error occurred).  Human
    review is needed to decide next steps.
    """
    marker = _marker(
        EVENT_ADVISORY,
        assignment=assignment_id,
        machine=machine_name,
        repo=repo_name,
        issue=issue_number,
    )
    lines = [
        "## Coordinator: Advisory — Worker Exited With 0 Commits",
        marker,
        f"**Machine:** {machine_name}",
        f"**Status:** advisory",
        f"**Duration:** {_fmt_duration(duration_seconds)}",
    ]
    if log_path:
        lines.append(f"**Log:** `{log_path}`")
    lines.append("")
    lines.append(
        "The worker exited cleanly (exit code 0) but pushed **no commits**. "
        "This typically means the feature was already implemented or no change "
        "was needed. Human review is required to decide the next step."
    )
    if reason.strip():
        lines.append("")
        lines.append("### Worker note")
        lines.append(reason.strip())
    return "\n".join(lines)


def format_plan(
    *,
    assignment_id: str,
    machine_name: str,
    repo_name: str,
    issue_number: int,
    plan: object,  # coord.plan_parser.WorkerPlan (untyped to avoid circular import)
    duration_seconds: float | None = None,
) -> str:
    """Build the issue comment posted when a plan-only assignment completes.

    *plan* should be a :class:`coord.plan_parser.WorkerPlan` instance, but the
    parameter is typed as ``object`` to avoid a circular import — callers that
    already have the dataclass can pass it directly.
    """
    marker = _marker(
        EVENT_PLAN,
        assignment=assignment_id,
        machine=machine_name,
        repo=repo_name,
        issue=issue_number,
    )
    lines = [
        "## Coordinator: Implementation Plan",
        marker,
        f"**Machine:** {machine_name}",
        f"**Duration:** {_fmt_duration(duration_seconds)}",
        "",
    ]

    plan_text: str = getattr(plan, "plan", "") or ""
    files_read: list[str] = getattr(plan, "files_read", []) or []
    files_modify: list[str] = getattr(plan, "files_modify", []) or []
    approach: str = getattr(plan, "approach", "") or ""
    risks: str = getattr(plan, "risks", "") or ""
    estimate: str = getattr(plan, "estimate", "") or ""

    if plan_text.strip():
        lines.append("### Summary")
        lines.append(plan_text.strip())
        lines.append("")

    if files_read:
        lines.append("### Files Read")
        lines.append(", ".join(f"`{f}`" for f in files_read))
        lines.append("")

    if files_modify:
        lines.append("### Files to Modify")
        lines.append(", ".join(f"`{f}`" for f in files_modify))
        lines.append("")

    if approach.strip():
        lines.append("### Approach")
        lines.append(approach.strip())
        lines.append("")

    if risks.strip():
        lines.append("### Risks")
        lines.append(risks.strip())
        lines.append("")

    if estimate.strip():
        lines.append("### Estimate")
        lines.append(estimate.strip())

    return "\n".join(lines)
