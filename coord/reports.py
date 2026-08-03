"""Report engine (#1742) — a registry of named, parameterised reports folded
out of the coordinator's own history.

The point is to stop paying an Opus coordinator session to hand-roll the same
aggregation every morning.  "What did the fleet do overnight, and where did it
all end up?" is pure deterministic arithmetic over the audit trail (#1036 /
#1037) — this module makes it a `coord report run` away, and reproducible.

Three layers, deliberately separated so the interesting one is testable:

1. :func:`fold_issue_activity` — **pure**.  Takes already-fetched audit
   entries plus an explicit ``(start, end)`` window and returns a
   :class:`ReportResult`.  No daemon, no DB, no clock (``generated_at``
   defaults to the window end).  This is where every derivation lives, and
   it unit-tests against fixture events.
2. :func:`fetch_audit_window` — pagination.  The audit read path hard-caps a
   single call at 500 rows (``coord.audit.MAX_LIMIT``); that is a *page
   size*, not a window bound, so this walks the keyset cursor until the
   window is covered and reports ``truncated=True`` if it genuinely could
   not finish.  Never silently drops the tail (#1742: "no silent caps").
3. :data:`REPORTS` + :func:`run_report` — the registry and its parameter
   validation.  One entry: ``issue-activity``.

The :class:`ReportResult` field names are the **wire contract** the coord-tui
Reports panel (#1741) renders against, and the CLI's ``--json`` and the
daemon's ``GET /report/{id}`` both emit exactly this shape — treat them as
public.

Read-only by construction: every query here is a ``SELECT``.  Running a
report must never touch the board (this repo has a recurring
"``reconcile()`` accretes behaviour" problem; reports do not join it).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "ReportError",
    "UnknownReportError",
    "ReportParam",
    "ReportDef",
    "ReportResult",
    "REPORTS",
    "catalogue",
    "resolve_params",
    "run_report",
    "fetch_audit_window",
    "fold_issue_activity",
    "run_issue_activity",
    "parse_duration",
]


class ReportError(ValueError):
    """A bad request against the report engine — unknown parameter, bad value.

    Callers (the CLI, the daemon) turn this into a clean message + non-zero
    exit / 400, never a traceback.
    """


class UnknownReportError(ReportError):
    """The requested ``report_id`` is not in :data:`REPORTS` (daemon: 404)."""


# ── parameter / definition / result shapes ─────────────────────────────────


@dataclass(frozen=True)
class ReportParam:
    """One parameter of a report, described richly enough that a client can
    build its input form from the catalogue alone (#1741 must NOT hardcode
    the param list).

    ``kind`` is ``"choice"`` (render a picker over ``choices``) or ``"text"``
    (render a free-text field).  ``free_form`` marks a ``choice`` param whose
    ``choices`` are *presets* rather than a whitelist — ``since`` is one:
    ``13h`` is a perfectly good window that nobody wants in a five-item
    picker.  ``validate`` is the server-side check, and is the authority; a
    client's form is a convenience on top of it.
    """

    id: str
    label: str
    kind: str = "text"
    choices: tuple[str, ...] = ()
    default: str = ""
    help: str = ""
    free_form: bool = False
    # Not part of the wire shape — the server-side validator. Raises
    # ReportError (message names the allowed values) on a bad value.
    validate: Callable[[str], None] | None = field(default=None, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "choices": list(self.choices),
            "default": self.default,
            "help": self.help,
            "free_form": self.free_form,
        }


@dataclass(frozen=True)
class ReportDef:
    """A named report.  ``run(**params)`` returns a :class:`ReportResult`."""

    id: str
    title: str
    description: str
    params: tuple[ReportParam, ...]
    run: Callable[..., "ReportResult"] = field(compare=False)

    def to_dict(self) -> dict[str, Any]:
        """Catalogue entry — everything a client needs except the callable."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "params": [p.to_dict() for p in self.params],
        }


@dataclass
class ReportResult:
    """The wire contract (#1741 renders against these exact field names).

    ``columns`` is the ordered list of row keys worth putting in a table;
    ``rows`` may carry extra keys beyond it (``started_before_window``,
    ``last_event_at``, ...) for clients that want the detail.  ``notes``
    holds derived anomalies and caveats, rendered under the table.
    """

    report_id: str
    generated_at: float
    window: tuple[float, float]
    columns: list[str]
    rows: list[dict]
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "generated_at": self.generated_at,
            "window": [self.window[0], self.window[1]],
            "columns": list(self.columns),
            "rows": list(self.rows),
            "notes": list(self.notes),
        }


# ── time helpers ───────────────────────────────────────────────────────────

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}


def parse_duration(raw: str) -> float:
    """``"13h"`` → ``46800.0``.  Units: s, m, h, d, w.  Raises ReportError."""
    match = _DURATION_RE.match(raw or "")
    if match is None:
        raise ReportError(
            f"not a duration: {raw!r} — expected e.g. '90m', '13h', '3d' "
            "(units: s, m, h, d, w)"
        )
    return float(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]


def parse_timestamp(raw: str) -> float:
    """Epoch seconds or ISO-8601 → float.  Mirrors ``coord audit``'s parsing
    so ``--param until=...`` and ``coord audit --until`` agree."""
    try:
        return float(raw)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise ReportError(
            f"not an epoch number or ISO-8601 timestamp: {raw!r}"
        ) from exc


def _iso(ts: float | None) -> str:
    if ts is None:
        return "?"
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%SZ"
    )


# ── parameter resolution ───────────────────────────────────────────────────


def resolve_params(report: ReportDef, raw: Mapping[str, Any] | None) -> dict[str, str]:
    """Validate ``raw`` against ``report.params`` and fill in defaults.

    Unknown keys and bad values raise :class:`ReportError` with a message
    that names what *was* allowed — the CLI and the daemon both surface it
    verbatim, so it has to read well on its own.
    """
    raw = dict(raw or {})
    known = {p.id: p for p in report.params}
    for key in raw:
        if key not in known:
            raise ReportError(
                f"unknown parameter {key!r} for report {report.id!r} — "
                f"known parameters: {', '.join(sorted(known)) or '(none)'}"
            )
    resolved: dict[str, str] = {}
    for param in report.params:
        value = raw.get(param.id)
        value = param.default if value is None or value == "" else str(value)
        _validate_param(param, value)
        resolved[param.id] = value
    return resolved


def _validate_param(param: ReportParam, value: str) -> None:
    if param.validate is not None:
        param.validate(value)
        return
    if param.kind == "choice" and param.choices and not param.free_form:
        if value not in param.choices:
            raise ReportError(
                f"invalid value for {param.id!r}: {value!r} — "
                f"allowed values: {', '.join(param.choices)}"
            )


# ── audit fetch + pagination ───────────────────────────────────────────────

# 100 pages x 500 rows = 50k events. Far past any real window; a backstop
# against an infinite cursor walk, not a coverage limit — hitting it sets
# truncated=True and the report says so in `notes`.
MAX_PAGES = 100


def _default_fetch(**kwargs: Any) -> dict:
    from coord.audit import query_audit_log  # noqa: PLC0415

    return query_audit_log(**kwargs)


def fetch_audit_window(
    *,
    since: float,
    until: float,
    repo: str | None = None,
    fetch: Callable[..., Mapping[str, Any]] | None = None,
    page_limit: int | None = None,
    max_pages: int = MAX_PAGES,
) -> tuple[list[dict], bool]:
    """Walk the keyset cursor until the whole ``[since, until]`` window is
    covered.  Returns ``(entries, truncated)``.

    ``truncated`` is True only when the walk gave up with rows still
    outstanding (page cap hit, or a page claimed ``has_more`` but handed
    back no cursor) — the caller turns that into an explicit note rather
    than shipping a silently short answer.
    """
    if fetch is None:
        fetch = _default_fetch
    if page_limit is None:
        from coord.audit import MAX_LIMIT  # noqa: PLC0415

        page_limit = MAX_LIMIT

    entries: list[dict] = []
    cursor: str | None = None
    truncated = True  # flipped to False the moment a page says "that's all"
    for _ in range(max(1, int(max_pages))):
        page = fetch(
            since=since,
            until=until,
            repo=repo or None,
            limit=page_limit,
            cursor=cursor,
        ) or {}
        entries.extend(page.get("entries") or [])
        if not page.get("has_more"):
            truncated = False
            break
        cursor = page.get("next_cursor")
        if not cursor:
            # has_more with no cursor — can't advance; stop rather than loop.
            break
    return entries, truncated


# ── issue-activity: the fold ───────────────────────────────────────────────

ISSUE_ACTIVITY_COLUMNS = [
    "repo",
    "issue",
    "title",
    "started_at",
    "machines",
    "fix_iterations",
    "test_verdicts",
    "review_verdicts",
    "merged_at",
    "drive_exit",
    "outcome",
]

_TEST_EVENTS = ("test_passed", "test_failed", "test_skipped")
_REVIEW_EVENTS = ("review_approve", "review_request-changes")

# An issue with no drive_exit and no event for this long by the end of the
# window is called `stalled` rather than `in-flight`. Two hours is well past
# any normal gate turnaround in this fleet.
STALL_QUIET_SECONDS = 2 * 3600.0

# A work-like dispatch is a real attempt at the issue; a review/smoke/plan
# dispatch is not, and must not count as a fix iteration.
_WORK_LIKE_TYPES = frozenset({"work", "mock-author", "test-author"})


def fold_issue_activity(
    entries: Iterable[Mapping[str, Any]],
    window: tuple[float, float],
    *,
    titles: Mapping[tuple[str, int], str] | None = None,
    generated_at: float | None = None,
    truncated: bool = False,
) -> ReportResult:
    """Fold audit entries into one row per ``(repo, issue)``.

    **Pure** — no DB, no daemon, no clock.  ``generated_at`` defaults to the
    window end so a frozen-clock test gets a deterministic result.

    ``entries`` may arrive in any order (the audit read path is newest-first);
    they are sorted ascending on ``(ts, id)`` here, which is what makes
    "first dispatch", "last merge" and the ordered verdict lists mean what
    they say.
    """
    start, end = float(window[0]), float(window[1])
    title_map = dict(titles or {})

    usable: list[Mapping[str, Any]] = []
    orphans = 0
    for entry in entries:
        if entry.get("repo") and entry.get("issue") is not None:
            usable.append(entry)
        else:
            orphans += 1
    usable.sort(key=lambda e: (float(e.get("ts") or 0.0), int(e.get("id") or 0)))

    groups: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    for entry in usable:
        key = (str(entry["repo"]), int(entry["issue"]))
        groups.setdefault(key, []).append(entry)

    rows = [
        _fold_one_issue(repo, issue, evs, end, title_map.get((repo, issue)))
        for (repo, issue), evs in groups.items()
    ]
    # Most-recently-active first: the morning question is "what moved", and
    # the thing that moved last is the thing still moving.
    rows.sort(key=lambda r: r["last_event_at"] or 0.0, reverse=True)

    notes: list[str] = []
    if truncated:
        notes.append(
            f"TRUNCATED: the window {_iso(start)} → {_iso(end)} could not be "
            f"fully fetched from the audit trail ({len(usable) + orphans} "
            "events read before the page cap). Rows below cover only part of "
            "the window — narrow it with a smaller `since` for a complete "
            "answer."
        )
    if orphans:
        notes.append(
            f"{orphans} event(s) in the window carry no repo/issue "
            "(fleet-level housekeeping) and are not represented in any row."
        )
    notes.extend(_derive_notes(rows))

    return ReportResult(
        report_id="issue-activity",
        generated_at=end if generated_at is None else float(generated_at),
        window=(start, end),
        columns=list(ISSUE_ACTIVITY_COLUMNS),
        rows=rows,
        notes=notes,
    )


def _fold_one_issue(
    repo: str,
    issue: int,
    events: Sequence[Mapping[str, Any]],
    window_end: float,
    title: str | None,
) -> dict[str, Any]:
    started_at: float | None = None
    machines: list[str] = []
    work_dispatches = 0
    test_verdicts: list[str] = []
    review_verdicts: list[str] = []
    merged_at: float | None = None
    drive_exit: dict[str, Any] | None = None

    for entry in events:
        category = entry.get("category")
        event_type = entry.get("event_type")
        ts = float(entry.get("ts") or 0.0)
        details = entry.get("details") or {}
        if not isinstance(details, Mapping):
            details = {}
        machine = entry.get("machine")
        if machine and machine not in machines:
            machines.append(machine)

        if category == "drive" and event_type == "drive_started":
            if started_at is None:
                started_at = ts
        elif category == "dispatch" and event_type == "dispatched":
            # `details.type` is absent on the oldest rows; "work" is the
            # assignment default, so that is the right assumption.
            if (details.get("type") or "work") in _WORK_LIKE_TYPES:
                work_dispatches += 1
                if started_at is None:
                    started_at = ts
        elif category == "test" and event_type in _TEST_EVENTS:
            test_verdicts.append(str(event_type)[len("test_"):])
        elif category == "review" and event_type in _REVIEW_EVENTS:
            review_verdicts.append(str(event_type)[len("review_"):])
        elif category == "merge" and event_type == "merged":
            merged_at = ts
        elif category == "drive" and event_type == "drive_exited":
            drive_exit = {
                "at": ts,
                "exit_code": details.get("exit_code"),
                "reason": details.get("reason") or details.get("error"),
            }

    # "In-window activity, but no start event in it" — the issue began
    # before the window opened. Reported as started_at=None + this flag
    # rather than as a bogus start time taken from the first event we
    # happened to see.
    started_before_window = started_at is None
    # Every work dispatch after the *first* one is a fix iteration. Note the
    # fold sees only in-window events by construction, so an issue whose
    # original dispatch predates the window and which was re-dispatched
    # inside it reads as "started here, zero fixes". That is the honest
    # answer available from the window; widen `since` to see the real start.
    fix_iterations = max(0, work_dispatches - 1)

    first_event_at = float(events[0].get("ts") or 0.0) if events else None
    last_event_at = float(events[-1].get("ts") or 0.0) if events else None

    return {
        "repo": repo,
        "issue": issue,
        "title": title,
        "started_at": started_at,
        "started_before_window": started_before_window,
        "machines": machines,
        "fix_iterations": fix_iterations,
        "test_verdicts": test_verdicts,
        "review_verdicts": review_verdicts,
        "merged_at": merged_at,
        "drive_exit": drive_exit,
        "outcome": _derive_outcome(merged_at, drive_exit, last_event_at, window_end),
        "first_event_at": first_event_at,
        "last_event_at": last_event_at,
        "event_count": len(events),
    }


def _nonzero_exit(drive_exit: Mapping[str, Any] | None) -> bool:
    """True when the driver did NOT exit clean.  A missing/None ``exit_code``
    counts — that shape is written by the crash path
    (``DriveRunner._drive_exit_summary``), which is exactly as unclean as a
    non-zero code."""
    return drive_exit is not None and drive_exit.get("exit_code") != 0


def _derive_outcome(
    merged_at: float | None,
    drive_exit: Mapping[str, Any] | None,
    last_event_at: float | None,
    window_end: float,
) -> str:
    if merged_at is not None:
        return "merged"
    if drive_exit is not None:
        # The driver is gone. Non-zero => it gave up loudly; clean exit with
        # nothing landed => it gave up quietly. Neither is in-flight.
        return "failed" if _nonzero_exit(drive_exit) else "stalled"
    if last_event_at is not None and (window_end - last_event_at) > STALL_QUIET_SECONDS:
        return "stalled"
    return "in-flight"


def _derive_notes(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Anomalies worth a human's eye, derived from the folded rows.

    The load-bearing one is the first: a driver that exits non-zero on an
    issue that then merges anyway.  That is the real 2026-08-02 case
    (#1631 exited 1 with "merge attempted 3 times without landing" at 21:48;
    the merge landed at 22:01) — a driver giving up on a merge that was
    still converging, otherwise invisible in both the event stream and the
    final board state.
    """
    notes: list[str] = []
    for row in rows:
        ident = f"{row['repo']}#{row['issue']}"
        drive_exit = row.get("drive_exit")
        if drive_exit and _nonzero_exit(drive_exit) and row.get("merged_at") is not None:
            reason = drive_exit.get("reason")
            reason_part = f" ({reason})" if reason else ""
            notes.append(
                f"{ident}: driver exited exit_code="
                f"{drive_exit.get('exit_code')!r} at {_iso(drive_exit.get('at'))}"
                f"{reason_part}, but the merge landed at "
                f"{_iso(row['merged_at'])} — the driver gave up on a merge "
                "that was still converging."
            )
        if (
            row.get("merged_at") is not None
            and row.get("test_verdicts")
            and row["test_verdicts"][-1] == "failed"
        ):
            notes.append(
                f"{ident}: merged at {_iso(row['merged_at'])} with the last "
                "in-window Test-gate verdict still 'failed'."
            )
        if int(row.get("fix_iterations") or 0) >= 3:
            notes.append(
                f"{ident}: {row['fix_iterations']} fix iterations in this "
                "window — the work is not converging on its own."
            )
    return notes


# ── issue-activity: the runner ─────────────────────────────────────────────


def _lookup_titles(
    keys: Iterable[tuple[str, int]],
) -> dict[tuple[str, int], str]:
    """Best-effort issue titles from the local DB.  Read-only, and failure is
    not an error — a missing title renders as ``None`` in the row, which is
    strictly better than failing the whole report over cosmetics."""
    keys = sorted(set(keys))
    if not keys:
        return {}
    out: dict[tuple[str, int], str] = {}
    try:
        from coord.db import get_connection  # noqa: PLC0415

        conn = get_connection()
        for repo, number in keys:
            row = conn.execute(
                "SELECT title FROM issues WHERE repo_name = ? AND number = ?",
                (repo, number),
            ).fetchone()
            if row is not None and row["title"]:
                out[(repo, number)] = row["title"]
                continue
            row = conn.execute(
                "SELECT issue_title FROM assignments WHERE repo_name = ? "
                "AND issue_number = ? AND issue_title IS NOT NULL "
                "ORDER BY rowid DESC LIMIT 1",
                (repo, number),
            ).fetchone()
            if row is not None and row["issue_title"]:
                out[(repo, number)] = row["issue_title"]
    except Exception:  # noqa: BLE001 — titles are cosmetic; never fail a report
        return out
    return out


def run_issue_activity(
    *,
    since: str = "24h",
    until: str = "",
    repo: str = "",
    now: float | None = None,
    fetch: Callable[..., Mapping[str, Any]] | None = None,
    title_lookup: Callable[..., Mapping[tuple[str, int], str]] | None = None,
) -> ReportResult:
    """Fetch the window (paginated) and fold it.  ``now``/``fetch``/
    ``title_lookup`` are test seams; the report's own parameters are
    ``since``/``until``/``repo``."""
    generated_at = time.time() if now is None else float(now)
    end = parse_timestamp(until) if until else generated_at
    start = end - parse_duration(since)

    entries, truncated = fetch_audit_window(
        since=start, until=end, repo=repo or None, fetch=fetch
    )
    lookup = _lookup_titles if title_lookup is None else title_lookup
    titles = lookup(
        (str(e["repo"]), int(e["issue"]))
        for e in entries
        if e.get("repo") and e.get("issue") is not None
    )
    return fold_issue_activity(
        entries,
        (start, end),
        titles=titles,
        generated_at=generated_at,
        truncated=truncated,
    )


# ── the catalogue ──────────────────────────────────────────────────────────

SINCE_PRESETS = ("1h", "6h", "24h", "3d", "7d")


def _validate_since(value: str) -> None:
    if value in SINCE_PRESETS:
        return
    try:
        parse_duration(value)
    except ReportError as exc:
        raise ReportError(
            f"invalid value for 'since': {value!r} — allowed values: "
            f"{', '.join(SINCE_PRESETS)}, or any duration like '13h' "
            "(units: s, m, h, d, w)"
        ) from exc


def _validate_until(value: str) -> None:
    if not value:
        return
    try:
        parse_timestamp(value)
    except ReportError as exc:
        raise ReportError(
            f"invalid value for 'until': {value!r} — expected epoch seconds "
            "or an ISO-8601 timestamp (e.g. '2026-08-03T09:16:00Z'), or "
            "empty for 'now'"
        ) from exc


ISSUE_ACTIVITY = ReportDef(
    id="issue-activity",
    title="Issue Activity",
    description=(
        "What moved in this window and where did it end up — the audit trail "
        "folded into one row per issue: when it started, which machines "
        "touched it, how many fix iterations it took, its Test/Review "
        "verdicts in order, whether it merged, and how its driver exited."
    ),
    params=(
        ReportParam(
            id="since",
            label="Time range",
            kind="choice",
            choices=SINCE_PRESETS,
            default="24h",
            help="How far back the window reaches from `until`. Presets, or any duration (e.g. 13h).",
            free_form=True,
            validate=_validate_since,
        ),
        ReportParam(
            id="until",
            label="Window end",
            kind="text",
            default="",
            help="Epoch seconds or ISO-8601. Empty means now.",
            validate=_validate_until,
        ),
        ReportParam(
            id="repo",
            label="Repo",
            kind="text",
            default="",
            help="Restrict to one repo by name. Empty means all repos.",
        ),
    ),
    run=run_issue_activity,
)


REPORTS: dict[str, ReportDef] = {ISSUE_ACTIVITY.id: ISSUE_ACTIVITY}


def catalogue() -> dict[str, Any]:
    """The wire shape of ``GET /report`` — everything #1741 needs to build a
    report picker and its parameter form without hardcoding anything."""
    return {"reports": [REPORTS[rid].to_dict() for rid in sorted(REPORTS)]}


def run_report(
    report_id: str,
    params: Mapping[str, Any] | None = None,
    **injected: Any,
) -> ReportResult:
    """Look up, validate, run.  Raises :class:`UnknownReportError` /
    :class:`ReportError` — never a traceback for a bad request."""
    report = REPORTS.get(report_id)
    if report is None:
        raise UnknownReportError(
            f"unknown report {report_id!r} — known reports: "
            f"{', '.join(sorted(REPORTS))}"
        )
    resolved = resolve_params(report, params)
    return report.run(**resolved, **injected)
