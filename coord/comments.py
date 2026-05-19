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
