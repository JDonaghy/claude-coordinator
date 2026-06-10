"""Format and parse coordinator-authored issue comments.

Comments are the message bus between the coordinator and human reviewers. They
need to be readable in GitHub's UI *and* parseable by future automation, so
each comment carries an HTML-comment marker with key=value metadata.

Marker grammar: `<!-- coord:event=<event> assignment=<id> ... -->`
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

EVENT_BRIEFING = "briefing"
EVENT_COMPLETION = "completion"
EVENT_FAILURE = "failure"
EVENT_STUCK = "stuck"
EVENT_PLAN = "plan"
EVENT_ADVISORY = "advisory"


@dataclass
class CommentMarker:
    event: str
    fields: dict[str, str]


_MARKER_RE = re.compile(r"<!--\s*coord:(?P<body>[^>]*?)\s*-->")
_FIELD_RE = re.compile(r"(\w+)=(\S+)")


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
    assignment_type: str = "work",
) -> str:
    """Format an advisory comment for a 0-commit clean exit.

    Distinct from both completion (no code was produced) and failure (the
    worker exited cleanly — exit_code 0 — so no error occurred).  Human
    review is needed to decide next steps.

    The default-case explanation assumes a ``work`` task ("already implemented
    or no change was needed").  For ``conflict-fix`` and other assignment
    types, the message is rewritten to match what 0 commits actually means
    for that flow (the rebase did not resolve anything).
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
    # #448 fix-iter-2: the default explanation only fits work tasks.  Pick a
    # message that matches the assignment type so a conflict-fix advisory
    # isn't mis-described as "feature already implemented".
    if assignment_type == "conflict-fix":
        lines.append(
            "The conflict-fix worker exited cleanly (exit code 0) but pushed "
            "**no commits**. The automated rebase did not resolve the conflict, "
            "so the parent merge has been flagged as requiring manual "
            "resolution. Human review is required to rebase the branch by hand."
        )
    else:
        lines.append(
            "The worker exited cleanly (exit code 0) but pushed **no commits**. "
            "This typically means the feature was already implemented or no "
            "change was needed. Human review is required to decide the next step."
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
