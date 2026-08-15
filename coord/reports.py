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
   validation.  Four entries: ``issue-activity``; ``drive-queue-status``
   (#1805), a **live snapshot** of ``drive_queue`` (no window, no audit
   trail, no clock beyond ``generated_at``) rather than a fold over history;
   and ``usage`` (#1763), a cost/token fold over board assignment rows that
   delegates every number to :mod:`coord.usage_rollup` priced with the
   daemon's own loaded ``pricing:`` config — the report that replaced
   coord-tui's ``panel:usage`` and its hardcoded pricing snapshot; and
   ``queue-outcomes`` (#2270), the one number the morning report is for —
   *what fraction of the queue got over the line without a human* — folded
   from #2235's per-host block log rather than from the audit trail, and the
   only report here that refuses to answer at all when its input file is not
   on this host.

The :class:`ReportResult` field names are the **wire contract** the coord-tui
Reports panel (#1741) renders against, and the CLI's ``--json`` and the
daemon's ``GET /report/{id}`` both emit exactly this shape — treat them as
public.

Read-only by construction: every query here is a ``SELECT``.  Running a
report must never touch the board (this repo has a recurring
"``reconcile()`` accretes behaviour" problem; reports do not join it).
"""

from __future__ import annotations

import csv
import io
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
    "ColumnMeta",
    "ChartSeries",
    "ChartSpec",
    "CHART_KINDS",
    "ReportResult",
    "REPORTS",
    "catalogue",
    "resolve_params",
    "run_report",
    "fetch_audit_window",
    "detect_prior_activity",
    "fold_issue_activity",
    "run_issue_activity",
    "fold_drive_queue_status",
    "run_drive_queue_status",
    "resolve_usage_window",
    "fold_usage",
    "run_usage",
    "QUEUE_OUTCOMES_COLUMNS",
    "QUEUE_OUTCOMES_COLUMN_META",
    "QUEUE_OUTCOMES_WINDOW_CHOICES",
    "resolve_queue_outcomes_window",
    "fold_queue_outcomes",
    "queue_outcomes_chart",
    "run_queue_outcomes",
    "parse_duration",
    "result_to_csv",
    "csv_filename",
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


@dataclass(frozen=True)
class ColumnMeta:
    """Display metadata for one entry of ``ReportResult.columns`` (#1760).

    Additive, not a retype: ``columns`` stays a bare ``list[str]`` (the
    already-shipped #1741 panel deserialises it as ``Vec<String>`` and must
    keep working unchanged), and row values stay raw — a ``started_at`` cell
    is still an epoch float, a ``machines`` cell is still a list.  This is
    only the hint a generic renderer needs to turn that raw value into a
    reasonable cell: ``kind`` says how to format it, ``align``/``weight``
    say how to lay out the column.  ``id`` matches the corresponding
    ``columns[]`` entry (and order matches too), so a client can zip them.
    """

    id: str
    label: str
    # Open vocabulary — a client that meets a `kind` it predates must fall
    # back to plain stringification, never fail to parse:
    # "text" | "int" | "timestamp" | "list" | "enum" | "duration" | "money"
    kind: str
    align: str = "left"  # "left" | "right"
    weight: float = 1.0  # relative column width hint

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "align": self.align,
            "weight": self.weight,
        }


# ── chart declaration (#2271) ──────────────────────────────────────────────
#
# A report says "this table also reads as a chart"; it does NOT ship a second
# copy of the numbers.  Every series names a `columns[]` id and the renderer
# reads the same `rows` the table renders, so there is exactly one source of
# truth, the table stays the fallback rendering, and `result_to_csv` (#1765)
# needs no change at all — it is driven by `columns`/`rows`/`totals` and never
# looks at this block.
#
# THE COMPATIBILITY RULE, same as `ColumnMeta.kind`'s (#1760): a client that
# does not understand this block, or meets a `kind` it predates, **renders the
# table and ignores the chart**.  It must never fail to parse and must never
# leave a hole where the chart would have gone.  That matters more than usual
# here because coord-tui ships as a per-host locally-built binary, outside
# propagation's reach, so the fleet routinely runs mixed versions.

#: Open vocabulary — the kinds a client is *expected* to know today.  A newer
#: daemon may name one that is not here; see the compatibility rule above.
CHART_KINDS = ("bar", "line", "sparkline")


@dataclass(frozen=True)
class ChartSeries:
    """One series of a :class:`ChartSpec`, derived from an existing column.

    ``column`` is a ``ReportResult.columns`` id whose per-row value supplies
    the y-values; ``label`` is what the legend shows.  ``color`` is an
    optional ``"#rrggbb"`` hint — when :attr:`ChartSpec.group_by` is set the
    series are generated per group and the backend palette picks the colours
    instead, because one declared colour cannot describe N groups.
    """

    label: str
    column: str
    color: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "column": self.column, "color": self.color}


@dataclass(frozen=True)
class ChartSpec:
    """An optional chart rendering of a :class:`ReportResult`'s own rows.

    Two shapes, and which one you get depends on ``group_by``:

    * **``group_by is None`` — one data point per row, in the report's
      canonical row order.**  ``x`` names the column supplying each point's
      category/time label; each :class:`ChartSeries` reads its own column
      straight off the row.  This is the "one bar per category" shape.
    * **``group_by`` set — a pivot.**  The x-axis is the *distinct* values of
      ``x`` in first-appearance order, and one output series is produced per
      distinct ``group_by`` value.  Rows landing in the same ``(group, x)``
      cell are **summed**, and an empty cell is ``0`` — so this shape is for
      magnitudes (counts, totals), not for averages or rates.  This is the
      "one trendline per bucket" shape that a long-form result needs.

    ``stacked`` is bar-only and ignored by every other kind.  Rendering a
    multi-series bar chart at all needs quadraui#584; a client whose pinned
    build predates it must degrade the section to a table with a stated
    reason rather than draw a chart that silently omits every series but the
    first.
    """

    kind: str
    series: tuple[ChartSeries, ...]
    x: str | None = None
    group_by: str | None = None
    stacked: bool = False
    title: str = ""
    y_label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "series": [s.to_dict() for s in self.series],
            "x": self.x,
            "group_by": self.group_by,
            "stacked": self.stacked,
            "title": self.title,
            "y_label": self.y_label,
        }


@dataclass
class ReportResult:
    """The wire contract (#1741 renders against these exact field names).

    ``columns`` is the ordered list of row keys worth putting in a table;
    ``rows`` may carry extra keys beyond it (``started_before_window``,
    ``last_event_at``, ...) for clients that want the detail.  ``notes``
    holds derived anomalies and caveats, rendered under the table.
    ``column_meta`` is additive display metadata, one entry per ``columns``
    entry in the same order (#1760) — a client that ignores it entirely
    still gets byte-identical ``columns``/``rows``.

    ``totals`` (#1763) is an optional grand-total row for reports that are a
    *fold* with a meaningful sum (``usage``), keyed by the same column ids as
    ``rows``.  It is **additive and defaults to ``None``**: reports that have
    no meaningful total (``issue-activity``, ``drive-queue-status``) leave it
    unset, and a client that ignores the key renders exactly as it did
    before.  Identity columns are deliberately *absent* from the dict rather
    than filled with a placeholder — a renderer that wants a ``Σ`` marker
    picks one itself, and one that doesn't leaves the cell blank.

    ``chart`` (#2271) is an optional declaration that this result also reads
    as a chart, derived from the very columns the table renders — see
    :class:`ChartSpec`.  **Additive and defaulting to ``None``**, and a
    client that ignores the key (or meets a ``kind`` it predates) renders the
    table exactly as it did before.
    """

    report_id: str
    generated_at: float
    window: tuple[float, float]
    columns: list[str]
    rows: list[dict]
    notes: list[str]
    column_meta: list[ColumnMeta] = field(default_factory=list)
    totals: dict[str, Any] | None = None
    chart: ChartSpec | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "generated_at": self.generated_at,
            "window": [self.window[0], self.window[1]],
            "columns": list(self.columns),
            "column_meta": [m.to_dict() for m in self.column_meta],
            "rows": list(self.rows),
            "notes": list(self.notes),
            "totals": None if self.totals is None else dict(self.totals),
            "chart": None if self.chart is None else self.chart.to_dict(),
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
    category: str | None = None,
    event_type: str | None = None,
) -> tuple[list[dict], bool]:
    """Walk the keyset cursor until the whole ``[since, until]`` window is
    covered.  Returns ``(entries, truncated)``.

    ``truncated`` is True only when the walk gave up with rows still
    outstanding (page cap hit, or a page claimed ``has_more`` but handed
    back no cursor) — the caller turns that into an explicit note rather
    than shipping a silently short answer.

    ``category``/``event_type`` push the filter down into
    :func:`coord.audit.query_audit_log` rather than filtering the pages
    afterwards — a four-week window (``queue-outcomes``) is exactly where
    reading every row to keep a handful of ``merged`` ones would hit the page
    cap and report itself truncated for no reason.  They are passed to
    ``fetch`` **only when set**, so an injected fetch that predates them keeps
    receiving byte-identical kwargs.
    """
    if fetch is None:
        fetch = _default_fetch
    if page_limit is None:
        from coord.audit import MAX_LIMIT  # noqa: PLC0415

        page_limit = MAX_LIMIT

    filters: dict[str, Any] = {}
    if category:
        filters["category"] = category
    if event_type:
        filters["event_type"] = event_type

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
            **filters,
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

# One entry per ISSUE_ACTIVITY_COLUMNS entry, same order (#1760) — the
# display metadata a generic renderer (CLI table, coord-tui panel) needs to
# format a raw row value without hardcoding per-report field knowledge.
ISSUE_ACTIVITY_COLUMN_META = [
    ColumnMeta(id="repo", label="Repo", kind="text"),
    ColumnMeta(id="issue", label="Issue", kind="int", align="right"),
    ColumnMeta(id="title", label="Title", kind="text", weight=3.0),
    ColumnMeta(id="started_at", label="Started", kind="timestamp"),
    ColumnMeta(id="machines", label="Machines", kind="list"),
    ColumnMeta(id="fix_iterations", label="Fixes", kind="int", align="right"),
    ColumnMeta(id="test_verdicts", label="Tests", kind="list"),
    ColumnMeta(id="review_verdicts", label="Reviews", kind="list"),
    ColumnMeta(id="merged_at", label="Merged", kind="timestamp"),
    ColumnMeta(id="drive_exit", label="Drive Exit", kind="text"),
    ColumnMeta(id="outcome", label="Outcome", kind="enum"),
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
    prior_activity: frozenset[tuple[str, int]] = frozenset(),
) -> ReportResult:
    """Fold audit entries into one row per ``(repo, issue)``.

    **Pure** — no DB, no daemon, no clock.  ``generated_at`` defaults to the
    window end so a frozen-clock test gets a deterministic result.

    ``entries`` may arrive in any order (the audit read path is newest-first);
    they are sorted ascending on ``(ts, id)`` here, which is what makes
    "first dispatch", "last merge" and the ordered verdict lists mean what
    they say.

    ``prior_activity`` (#1760) is the one fact this pure fold cannot derive
    for itself: the set of ``(repo, issue)`` keys that have *any* audit event
    before the window opened, as determined by the caller's bounded
    look-back (:func:`detect_prior_activity`).  Without it, an issue whose
    real start predates the window but which was re-dispatched inside it
    reads as "started here, zero fixes" — a real timestamp and a real count
    that are both wrong, with nothing in the row saying so.  With it, that
    row instead reports ``started_at=None``, ``started_before_window=True``
    and ``counts_partial=True``, and every in-window work dispatch counts as
    a fix (the issue was already running when the window opened, so each one
    is a re-dispatch).  Default is empty, so existing callers are unaffected
    in shape.
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
        _fold_one_issue(
            repo,
            issue,
            evs,
            end,
            title_map.get((repo, issue)),
            had_prior_activity=(repo, issue) in prior_activity,
        )
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
        column_meta=list(ISSUE_ACTIVITY_COLUMN_META),
        rows=rows,
        notes=notes,
    )


def _fold_one_issue(
    repo: str,
    issue: int,
    events: Sequence[Mapping[str, Any]],
    window_end: float,
    title: str | None,
    *,
    had_prior_activity: bool = False,
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

    if had_prior_activity:
        # The caller's look-back (#1760) found an event before the window
        # opened — this issue was already running. Report that plainly
        # rather than claiming a start the window cannot support: no start
        # time, and every in-window work dispatch is a re-dispatch (not
        # "first dispatch, zero fixes").
        started_at = None
        started_before_window = True
        fix_iterations = work_dispatches
    else:
        # "In-window activity, but no start event in it" — the issue began
        # before the window opened. Reported as started_at=None + this flag
        # rather than as a bogus start time taken from the first event we
        # happened to see.
        started_before_window = started_at is None
        # Every work dispatch after the *first* one is a fix iteration.
        fix_iterations = max(0, work_dispatches - 1)
    # counts_partial is narrower than started_before_window: the latter can
    # also fire from the plain "no start event in this window" inference
    # above, which doesn't know whether fix_iterations/test_verdicts are
    # complete or merely empty. Only a confirmed look-back hit means the
    # counts are a known lower bound.
    counts_partial = had_prior_activity

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
        "counts_partial": counts_partial,
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
        if row.get("counts_partial"):
            # #1760: this issue was already running when the window opened
            # (the caller's look-back found an earlier event) — say so
            # explicitly rather than let a real-looking fix_iterations/
            # test_verdicts count pass as complete.
            notes.append(
                f"{ident}: started before this window — fix_iterations and "
                "test_verdicts are lower bounds, not the full count. Widen "
                "`since` to see the real start."
            )
        elif (
            "request-changes" in (row.get("review_verdicts") or [])
            and int(row.get("fix_iterations") or 0) == 0
        ):
            # #1760: a request-changes verdict implies at least one
            # re-dispatch happened. fix_iterations=0 with counts_partial
            # False (the elif) means the fold believes it saw the whole
            # window's activity — this combination should not be reachable,
            # and if it appears the row is self-contradictory.
            notes.append(
                f"{ident}: review verdict 'request-changes' with "
                "fix_iterations=0 — this combination should not happen; the "
                "row is internally inconsistent."
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


def detect_prior_activity(
    keys: Iterable[tuple[str, int]],
    *,
    until: float,
    fetch: Callable[..., Mapping[str, Any]],
) -> frozenset[tuple[str, int]]:
    """Bounded look-back (#1760): which ``(repo, issue)`` keys already have
    at least one audit event before ``until`` (the window start)?

    One query per issue in ``keys`` — not one per event, not an unbounded
    scan.  Same query path as the window fetch (``fetch`` is the same
    callable, real or injected), just ``until=window_start``, a per-issue
    filter, and ``limit=1`` newest-first: the fold only needs a yes/no per
    issue, not the events themselves.
    """
    prior: set[tuple[str, int]] = set()
    for key_repo, key_issue in sorted(set(keys)):
        page = fetch(
            since=None,
            until=until,
            repo=key_repo,
            issue=key_issue,
            limit=1,
            cursor=None,
        ) or {}
        if page.get("entries"):
            prior.add((key_repo, key_issue))
    return frozenset(prior)


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

    fetch_fn = _default_fetch if fetch is None else fetch

    entries, truncated = fetch_audit_window(
        since=start, until=end, repo=repo or None, fetch=fetch_fn
    )
    keys = {
        (str(e["repo"]), int(e["issue"]))
        for e in entries
        if e.get("repo") and e.get("issue") is not None
    }
    lookup = _lookup_titles if title_lookup is None else title_lookup
    titles = lookup(keys)
    prior_activity = detect_prior_activity(keys, until=start, fetch=fetch_fn)
    return fold_issue_activity(
        entries,
        (start, end),
        titles=titles,
        generated_at=generated_at,
        truncated=truncated,
        prior_activity=prior_activity,
    )


# ── drive-queue-status: a live snapshot, not a fold ────────────────────────
#
# #1805: "what is queued, and is it moving?" without a CLI round-trip.  Unlike
# issue-activity this is not a fold over an audit-trail window — it is a
# point-in-time read of `drive_queue` via `coord.state.list_drive_queue`
# (daemon-or-local already handled there), so `window` is degenerate:
# `(generated_at, generated_at)`.  `drive_queue` has no `completed_at` and
# `coord/drive_queue.py` emits no audit events, so there is no data source
# for a queue *history* report — see this issue's "Out of scope".

DRIVE_QUEUE_STATUS_COLUMNS = [
    "position",
    "repo",
    "issue",
    "title",
    "state",
    "machine",
    "attempts",
    "deferrals",
    "last_reason",
    "reason_at",
    "enqueued_at",
    "launched_at",
    "hold_state",
    "after",
]

# One entry per DRIVE_QUEUE_STATUS_COLUMNS entry, same order (#1760).
DRIVE_QUEUE_STATUS_COLUMN_META = [
    ColumnMeta(id="position", label="Pos", kind="int", align="right"),
    ColumnMeta(id="repo", label="Repo", kind="text"),
    ColumnMeta(id="issue", label="Issue", kind="int", align="right"),
    ColumnMeta(id="title", label="Title", kind="text", weight=2.0),
    ColumnMeta(id="state", label="State", kind="enum"),
    ColumnMeta(id="machine", label="Machine", kind="text"),
    ColumnMeta(id="attempts", label="Attempts", kind="int", align="right"),
    ColumnMeta(id="deferrals", label="Deferrals", kind="int", align="right"),
    ColumnMeta(id="last_reason", label="Last Reason", kind="text", weight=3.0),
    # #2133: capture time of `last_reason` — a client renders it as an age
    # next to the reason so a stale snapshot never reads as current state.
    # `None`/absent for a row predating the migration.
    ColumnMeta(id="reason_at", label="Reason At", kind="timestamp"),
    ColumnMeta(id="enqueued_at", label="Enqueued", kind="timestamp"),
    ColumnMeta(id="launched_at", label="Launched", kind="timestamp"),
    ColumnMeta(id="hold_state", label="Hold", kind="enum"),
    ColumnMeta(id="after", label="After", kind="list"),
]

# The #1794 tell: an entry that has already burned at least one launch
# attempt is the thing an operator most wants shouted at them.
_RETRIED_ATTEMPTS_THRESHOLD = 1


def fold_drive_queue_status(
    entries: Iterable[Mapping[str, Any]],
    generated_at: float,
    *,
    titles: Mapping[tuple[str, int], str] | None = None,
    queue_escalation: Mapping[str, Any] | None = None,
) -> ReportResult:
    """Fold already-fetched ``drive_queue`` rows into a snapshot ``ReportResult``.

    **Pure** — no DB, no daemon, no clock: ``entries`` is whatever
    :func:`coord.state.list_drive_queue` returned (raw column names,
    ``after_json`` already decoded to a list) and ``generated_at`` is the
    caller's clock reading, reused verbatim for both ends of ``window`` since
    a live snapshot has no meaningful range.

    ``entries`` arrives pre-ordered (``list_drive_queue`` is
    ``ORDER BY position, id``) — this fold does not re-sort.
    """
    title_map = dict(titles or {})
    rows: list[dict[str, Any]] = []
    for entry in entries:
        repo = str(entry.get("repo_name") or "")
        issue = int(entry.get("issue_number") or 0)
        rows.append(
            {
                "position": int(entry.get("position") or 0),
                "repo": repo,
                "issue": issue,
                "title": title_map.get((repo, issue)),
                "state": entry.get("state") or "",
                "machine": entry.get("machine") or "",
                "attempts": int(entry.get("attempts") or 0),
                "deferrals": int(entry.get("deferrals") or 0),
                "last_reason": entry.get("last_reason") or "",
                "reason_at": entry.get("reason_at"),
                "enqueued_at": entry.get("enqueued_at"),
                "launched_at": entry.get("launched_at"),
                "hold_state": entry.get("hold_state") or "",
                "after": list(entry.get("after_json") or []),
                # Extra keys beyond `columns` — ReportResult's contract
                # explicitly allows this for clients that want the detail.
                "session_name": entry.get("session_name") or "",
                "hold_reason": entry.get("hold_reason") or "",
                "resume_when": entry.get("resume_when") or "",
                # #2186: without this, a report consumer (or `coord reports
                # drive-queue-status`) has the same blind spot the TUI had —
                # a fired gate with no way to tell "this entry alone is
                # held" from "the whole queue stopped". Fail-closed exactly
                # like `coord.state.list_drive_queue`'s own normalisation
                # (and `QueueEntry._normalize_hold_scope`): anything other
                # than the literal `"fleet"` reads as the narrower `"entry"`.
                "hold_scope": "fleet" if str(entry.get("hold_scope") or "") == "fleet" else "entry",
            }
        )

    notes: list[str] = []
    if not rows:
        notes.append("The drive queue is empty.")
    else:
        from coord.drive_queue import (  # noqa: PLC0415
            STATE_BLOCKED,
            STATE_DONE,
            STATE_FAILED,
            STATE_RUNNING,
            STATE_WAITING,
            TERMINAL_QUEUE_STATES,
        )

        counts: dict[str, int] = {}
        for r in rows:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        # The headline is entries the queue will still act on — `done` (and
        # any other terminal state) is run history, not queue depth, so it
        # is excluded here rather than folded into `len(rows)` (#1855).
        queued = sum(n for state, n in counts.items() if state not in TERMINAL_QUEUE_STATES)

        def _ordered(present: set[str], preferred: tuple[str, ...]) -> list[str]:
            # `preferred` is display polish only — any state absent from it
            # (a future addition to drive_queue.py's five, or one we simply
            # forgot to list) still surfaces, just alphabetically after the
            # known ones, so nothing can silently vanish the way `blocked`
            # did before this fix.
            return [s for s in preferred if s in present] + sorted(present - set(preferred))

        non_terminal_states = _ordered(
            {s for s in counts if s not in TERMINAL_QUEUE_STATES},
            (STATE_RUNNING, STATE_WAITING),
        )
        # `blocked`/`failed` are the states that need a human — call them
        # out ahead of the benign `done` count, not appended after it.
        terminal_states = _ordered(
            {s for s in counts if s in TERMINAL_QUEUE_STATES},
            (STATE_BLOCKED, STATE_FAILED, STATE_DONE),
        )

        breakdown = ", ".join(f"{counts[s]} {s}" for s in non_terminal_states)
        headline = f"{queued} entr{'y' if queued == 1 else 'ies'} queued"
        if breakdown:
            headline += f" ({breakdown})"
        terminal_parts = [f"{counts[s]} {s}" for s in terminal_states]
        if terminal_parts:
            headline += " · " + " · ".join(terminal_parts)
        notes.append(headline + ".")
        retried = [r for r in rows if r["attempts"] >= _RETRIED_ATTEMPTS_THRESHOLD]
        if retried:
            named = ", ".join(
                f"{r['repo']}#{r['issue']} (attempts={r['attempts']})" for r in retried
            )
            notes.append(f"attempts>=1: {named}.")
    if queue_escalation:
        reason = queue_escalation.get("reason") or "(no reason recorded)"
        stage = queue_escalation.get("stage") or "?"
        notes.append(
            f"standing queue-level escalation: stage={stage!r} — {reason}"
        )

    return ReportResult(
        report_id="drive-queue-status",
        generated_at=generated_at,
        window=(generated_at, generated_at),
        columns=list(DRIVE_QUEUE_STATUS_COLUMNS),
        column_meta=list(DRIVE_QUEUE_STATUS_COLUMN_META),
        rows=rows,
        notes=notes,
    )


def _default_list_drive_queue(repo: str | None) -> list[dict]:
    from coord.state import list_drive_queue  # noqa: PLC0415

    return list_drive_queue(repo)


def _default_queue_escalation() -> Mapping[str, Any] | None:
    """The standing queue-level escalation record (#1754's synthetic key),
    if one exists.  A plain read — never runs a tick — so it is safe to
    surface here; best-effort, mirroring :func:`_lookup_titles`."""
    try:
        from coord.drive_queue import (  # noqa: PLC0415
            QUEUE_ALERT_ISSUE,
            QUEUE_ALERT_REPO,
        )
        from coord.state import get_drive_escalation  # noqa: PLC0415

        return get_drive_escalation(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
    except Exception:  # noqa: BLE001 — cosmetic; never fail the report over it
        return None


def run_drive_queue_status(
    *,
    repo: str = "",
    now: float | None = None,
    fetch: Callable[[str | None], Sequence[Mapping[str, Any]]] | None = None,
    title_lookup: Callable[..., Mapping[tuple[str, int], str]] | None = None,
    escalation_lookup: Callable[[], Mapping[str, Any] | None] | None = None,
) -> ReportResult:
    """Fetch the live queue and fold it.  ``now``/``fetch``/``title_lookup``/
    ``escalation_lookup`` are test seams (mirrors :func:`run_issue_activity`);
    the report's own parameter is ``repo``.

    Read-only and tick-free by construction: the only call here is
    ``list_drive_queue`` (or the injected ``fetch``) — never ``plan_tick``.
    """
    generated_at = time.time() if now is None else float(now)
    fetch_fn = _default_list_drive_queue if fetch is None else fetch
    entries = list(fetch_fn(repo or None) or [])

    keys = {
        (str(e["repo_name"]), int(e["issue_number"]))
        for e in entries
        if e.get("repo_name") and e.get("issue_number") is not None
    }
    lookup = _lookup_titles if title_lookup is None else title_lookup
    titles = lookup(keys)

    esc_lookup = _default_queue_escalation if escalation_lookup is None else escalation_lookup
    queue_escalation = esc_lookup()

    return fold_drive_queue_status(
        entries,
        generated_at,
        titles=titles,
        queue_escalation=queue_escalation,
    )


# ── usage: the per-issue / per-repo cost + token rollup ────────────────────
#
# #1763.  This is a **correctness fix**, not a consolidation.  `coord-tui`'s
# `panel:usage` was a Rust port of `coord/usage_rollup.py` carrying a
# hardcoded snapshot of `coord.config.PricingConfig`'s shipped defaults, so
# an operator who overrode `pricing:` in coordinator.yml changed what
# `coord usage` reported and left the panel confidently showing different
# numbers (the durable #1116 finding).  The daemon holds the config, so the
# daemon does the arithmetic: everything below *calls* `usage_rollup.rollup`
# / `rollup_by_stage` with the loaded `PricingConfig` and reimplements none
# of its window predicate, leg-cost rule or default sort.

USAGE_WINDOW_CHOICES = ("today", "week", "month", "7d", "30d")
USAGE_GROUP_BY_CHOICES = ("issue", "repo")

# Columns depend on `group_by`: a repo-grouped row IS the whole repo, so it
# carries no issue number and no title (same shape the retired panel used).
USAGE_ISSUE_COLUMNS = [
    "issue",
    "repo",
    "title",
    "legs",
    "tokens_in",
    "tokens_out",
    "cost_captured",
    "cost_est",
    "cost_total",
]

USAGE_REPO_COLUMNS = [
    "repo",
    "legs",
    "tokens_in",
    "tokens_out",
    "cost_captured",
    "cost_est",
    "cost_total",
]

# #1760 display metadata, indexed by column id and emitted in `columns`
# order — a large `weight` on `title`, `int`/`right` for the counts, and
# `money`/`right` for the three dollar columns.  `money` is a *generic* kind
# (the vocabulary is open, see ColumnMeta): a client that predates it falls
# back to plain stringification and still shows the number.
_USAGE_COLUMN_META: dict[str, ColumnMeta] = {
    "issue": ColumnMeta(id="issue", label="Issue", kind="int", align="right", weight=0.8),
    "repo": ColumnMeta(id="repo", label="Repo", kind="text", weight=1.5),
    "title": ColumnMeta(id="title", label="Title", kind="text", weight=4.0),
    "legs": ColumnMeta(id="legs", label="Legs", kind="int", align="right", weight=0.6),
    "tokens_in": ColumnMeta(id="tokens_in", label="Tok In", kind="int", align="right"),
    "tokens_out": ColumnMeta(id="tokens_out", label="Tok Out", kind="int", align="right"),
    "cost_captured": ColumnMeta(
        id="cost_captured", label="Cost $", kind="money", align="right"
    ),
    "cost_est": ColumnMeta(id="cost_est", label="Est ~$", kind="money", align="right"),
    "cost_total": ColumnMeta(id="cost_total", label="Total $", kind="money", align="right"),
}

# Dollar figures are rounded before they go on the wire so a float artefact
# (2.8000000000000003) never reaches a generic renderer. Six places is far
# below any real per-leg cost and above any rounding that could change a
# reported cent.
_USAGE_COST_PLACES = 6


def usage_columns(group_by: str) -> list[str]:
    """The ``columns`` list for *group_by*.  Raises :class:`ReportError`."""
    if group_by == "issue":
        return list(USAGE_ISSUE_COLUMNS)
    if group_by == "repo":
        return list(USAGE_REPO_COLUMNS)
    raise ReportError(
        f"invalid value for 'group_by': {group_by!r} — "
        f"allowed values: {', '.join(USAGE_GROUP_BY_CHOICES)}"
    )


def resolve_usage_window(window: str, now: float | None = None):
    """Resolve a ``window`` parameter to a :class:`coord.usage_rollup.TimeWindow`.

    Every preset is *called* from :mod:`coord.usage_rollup`, never
    reimplemented — that module owns the calendar (this is precisely what the
    retired panel hand-rolled a civil calendar to duplicate).
    """
    from coord.usage_rollup import (  # noqa: PLC0415
        Window,
        window_month,
        window_today,
        window_week,
    )

    if window == "today":
        return window_today(now)
    if window == "week":
        return window_week(now)
    if window == "month":
        return window_month(now)
    if window in ("7d", "30d"):
        # Window.since is the *bounded* variant: [now - spec, now).
        return Window.since(window, now)
    raise ReportError(
        f"invalid value for 'window': {window!r} — "
        f"allowed values: {', '.join(USAGE_WINDOW_CHOICES)}"
    )


def _usage_row_title(leg_rows: Sequence[Mapping[str, Any]]) -> str | None:
    for row in leg_rows:
        title = row.get("issue_title")
        if title:
            return str(title)
    return None


def _usage_metrics(group: Any) -> dict[str, Any]:
    """The numeric half of a row (or of ``totals``) — identical for both."""
    return {
        "legs": int(group.legs),
        "tokens_in": int(group.tokens.input),
        "tokens_out": int(group.tokens.output),
        "cost_captured": round(float(group.cost_captured), _USAGE_COST_PLACES),
        "cost_est": round(float(group.cost_est), _USAGE_COST_PLACES),
        "cost_total": round(float(group.cost_total), _USAGE_COST_PLACES),
        # Beyond `columns` — the contract explicitly allows extra row keys,
        # and a client that wants the cache split or the open-leg count can
        # have it without another column in an already-wide table.
        "tokens_cache_read": int(group.tokens.cache_read),
        "tokens_cache_creation": int(group.tokens.cache_creation),
        "duration_secs": round(float(group.duration_secs), 3),
        "open_legs": int(group.open_legs),
        "unknown_model_legs": int(group.unknown_model_legs),
    }


def _usage_stage_breakdown(
    leg_rows: Sequence[Mapping[str, Any]], window: Any, pricing: Any
) -> list[dict[str, Any]]:
    """Per-stage sub-rollup for one group, as a list of plain dicts.

    The panel's only drill-down was "click a row → its per-stage legs"; that
    maps onto rows without needing a second request, so it ships inline as an
    extra row key rather than as a second report.
    """
    from coord.usage_rollup import rollup_by_stage  # noqa: PLC0415

    sub = rollup_by_stage(list(leg_rows), window, pricing)
    stages = [
        {"stage": str(key), **_usage_metrics(grp)} for key, grp in sub.groups.items()
    ]
    stages.sort(key=lambda s: s["cost_total"], reverse=True)
    return stages


def fold_usage(
    rows: Iterable[Mapping[str, Any]],
    window: Any,
    *,
    group_by: str = "issue",
    pricing: Any = None,
    generated_at: float | None = None,
    extra_notes: Sequence[str] = (),
) -> ReportResult:
    """Fold board assignment rows into a per-issue / per-repo cost rollup.

    **Pure** — no DB, no daemon, no clock: *rows* is whatever the caller
    fetched (daemon ``/board`` ``assignments`` wire shape), *window* is a
    resolved :class:`~coord.usage_rollup.TimeWindow`, and *pricing* is the
    :class:`~coord.config.PricingConfig` that was actually loaded.  Every
    number comes back out of :func:`coord.usage_rollup.rollup` — this
    function only shapes it into the report wire contract.

    *pricing* left at ``None`` falls through to ``usage_rollup``'s own
    built-in defaults, which is correct for a unit test and **not** what the
    runner does (see :func:`run_usage`, which loads ``coordinator.yml``).
    """
    from coord.usage_rollup import IssueKey, rollup  # noqa: PLC0415

    columns = usage_columns(group_by)
    rows = list(rows)
    result = rollup(rows, group_by=group_by, window=window, pricing=pricing)

    out_rows: list[dict[str, Any]] = []
    for key, group in result.groups.items():
        row: dict[str, Any] = {}
        if isinstance(key, IssueKey):
            row["issue"] = int(key.issue_number)
            row["repo"] = str(key.repo_name)
            row["title"] = _usage_row_title(group.leg_rows)
        else:
            row["repo"] = str(key)
        row.update(_usage_metrics(group))
        row["stages"] = _usage_stage_breakdown(group.leg_rows, window, pricing)
        out_rows.append(row)

    # Same default order as `coord usage` and the retired panel: biggest
    # spend first. `_ident` breaks ties deterministically so a frozen-clock
    # test isn't at the mercy of dict ordering.
    out_rows.sort(
        key=lambda r: (-r["cost_total"], str(r.get("repo") or ""), int(r.get("issue") or 0))
    )

    start = 0.0 if getattr(window, "start", None) is None else float(window.start)
    end = (
        float(generated_at if generated_at is not None else start)
        if getattr(window, "end", None) is None
        else float(window.end)
    )

    totals = _usage_metrics(result.total)

    notes: list[str] = list(extra_notes)
    if not out_rows:
        notes.append("No usage recorded in this window.")
    for row in out_rows:
        unknown = int(row.get("unknown_model_legs") or 0)
        if unknown:
            ident = (
                f"{row['repo']}#{row['issue']}" if "issue" in row else str(row["repo"])
            )
            notes.append(
                f"{ident}: {unknown} leg(s) ran a model with no entry in the "
                "loaded `pricing:` config — their tokens are counted but "
                "their spend is NOT in `cost_est` (never silently priced at "
                "$0). Add a rate for that model to coordinator.yml."
            )
    if totals["open_legs"]:
        notes.append(
            f"{totals['open_legs']} leg(s) in this window are still running — "
            "their duration counts as 0 and their cost is not final."
        )

    return ReportResult(
        report_id="usage",
        generated_at=end if generated_at is None else float(generated_at),
        window=(start, end),
        columns=columns,
        column_meta=[_USAGE_COLUMN_META[c] for c in columns],
        rows=out_rows,
        notes=notes,
        totals=totals,
    )


def _default_usage_rows(repo: str | None) -> list[dict]:  # noqa: ARG001
    """Board assignment rows from the local DB.

    Deliberately **not** :func:`coord.usage.fetch_usage_rows`: that helper
    branches to a ``GET /board`` when a board service is configured, and a
    report already runs *on* the daemon host (``coord.state.run_report``
    routes a thin client's request to ``GET /report/{id}``), so going through
    it would make the daemon HTTP-call itself.  This mirrors that helper's
    *local* branch exactly — ``list_assignments()`` rather than the
    retention-capped ``/board`` projection, because a usage rollup wants full
    history.
    """
    from coord.dao import SqliteStore  # noqa: PLC0415

    return SqliteStore().list_assignments()


def _load_pricing() -> tuple[Any, list[str]]:
    """The ``pricing:`` block from the loaded ``coordinator.yml``.

    Returns ``(PricingConfig, notes)``.  A config that cannot be loaded falls
    back to the built-in defaults **and says so in ``notes``** — silently
    falling back is exactly the failure mode #1763 exists to remove.
    """
    from coord.config import PricingConfig  # noqa: PLC0415

    try:
        from coord.config import load, resolve_config_path  # noqa: PLC0415

        return load(resolve_config_path()).pricing, []
    except Exception as exc:  # noqa: BLE001 — surfaced as a note, not a crash
        return (
            PricingConfig(),
            [
                "WARNING: coordinator.yml could not be loaded "
                f"({type(exc).__name__}: {exc}) — `cost_est` uses the built-in "
                "default rates, which may differ from this fleet's `pricing:` "
                "block."
            ],
        )


def run_usage(
    *,
    window: str = "today",
    group_by: str = "issue",
    repo: str = "",
    now: float | None = None,
    fetch: Callable[[str | None], Sequence[Mapping[str, Any]]] | None = None,
    pricing: Any = None,
) -> ReportResult:
    """Fetch board rows and fold them.  ``now``/``fetch``/``pricing`` are test
    seams; the report's own parameters are ``window``/``group_by``/``repo``."""
    generated_at = time.time() if now is None else float(now)
    resolved = resolve_usage_window(window, generated_at)

    fetch_fn = _default_usage_rows if fetch is None else fetch
    rows = list(fetch_fn(repo or None) or [])
    if repo:
        rows = [r for r in rows if str(r.get("repo_name") or "") == repo]

    extra_notes: list[str] = []
    if pricing is None:
        pricing, extra_notes = _load_pricing()

    return fold_usage(
        rows,
        resolved,
        group_by=group_by,
        pricing=pricing,
        generated_at=generated_at,
        extra_notes=extra_notes,
    )


# ── queue-outcomes: the morning number (#2270) ─────────────────────────────
#
# One question: **what fraction of the queue got over the line without me?**
# The operator's target is `(succeeded + auto_resolved_mechanism +
# auto_resolved_rescue) / total` trending to ~100%.
#
# This is the view over #2235's Phase-0 recorder (`coord.block_log`), which is
# the only durable record of a stall *and how it ended*.  `drive-queue-status`
# cannot answer it and says so in its own description ("a snapshot, not a
# history: `drive_queue` has no `completed_at`"), so nothing here reads the
# queue table.
#
# TWO SOURCES, and the seam between them is deliberate:
#
# * every bucket except `succeeded` folds out of block-log EPISODES, because a
#   stall is the only thing that log records; and
# * `succeeded` — merged with no stall at all — has no episode by
#   construction, so it is counted from `merged` audit events in the same
#   window, minus any key that already has an episode there (a stall that
#   later landed is auto_resolved, not succeeded, and must not be counted
#   twice).  The report says which number came from where in its notes; a
#   headline whose denominator is silently half-sourced is worse than none.

QUEUE_OUTCOMES_WINDOW_CHOICES = ("24h", "7d", "4w")

#: window -> (span, period).  The bar view is one period; the trend views are
#: 7 daily and 4 weekly points of the same arithmetic, so a client renders a
#: trendline by grouping rows on `period_start` and needs no second report.
_QUEUE_OUTCOMES_WINDOWS: dict[str, tuple[float, float]] = {
    "24h": (86400.0, 86400.0),
    "7d": (7 * 86400.0, 86400.0),
    "4w": (28 * 86400.0, 7 * 86400.0),
}

QUEUE_OUTCOMES_COLUMNS = [
    "period_start",
    "bucket",
    "category",
    "by_design",
    "count",
    "share_pct",
    "issues",
]

# One entry per QUEUE_OUTCOMES_COLUMNS entry, same order (#1760).
QUEUE_OUTCOMES_COLUMN_META = [
    ColumnMeta(id="period_start", label="Period", kind="timestamp"),
    ColumnMeta(id="bucket", label="Bucket", kind="enum", weight=1.6),
    ColumnMeta(id="category", label="Category", kind="text", weight=3.0),
    ColumnMeta(id="by_design", label="By Design", kind="text"),
    ColumnMeta(id="count", label="Count", kind="int", align="right", weight=0.6),
    ColumnMeta(id="share_pct", label="Share %", kind="text", align="right", weight=0.7),
    # Attributability (#2270 acceptance): every count drills to the exact
    # `(repo, issue)` list behind it. Truncated in a terminal table, whole in
    # --format json / csv.
    ColumnMeta(id="issues", label="Issues", kind="list", weight=3.0),
]

_MERGE_AUDIT_CATEGORY = "merge"
_MERGE_AUDIT_EVENT = "merged"


def resolve_queue_outcomes_window(
    window: str, end: float
) -> tuple[float, float, float]:
    """``(start, end, period_seconds)`` for a ``window`` preset.

    Periods are aligned to ``end``, not to the civil calendar: the report has
    to be reproducible from ``until`` alone, and a calendar alignment would
    make the same ``until`` produce different buckets in different timezones.
    """
    try:
        span, period = _QUEUE_OUTCOMES_WINDOWS[window]
    except KeyError:
        raise ReportError(
            f"invalid value for 'window': {window!r} — "
            f"allowed values: {', '.join(QUEUE_OUTCOMES_WINDOW_CHOICES)}"
        ) from None
    return float(end) - span, float(end), period


def _period_bounds(start: float, end: float, period: float) -> list[float]:
    """The start timestamp of each period in ``[start, end)``, ascending."""
    if period <= 0:
        return [start]
    count = max(1, int(round((end - start) / period)))
    return [start + i * period for i in range(count)]


def _period_index(ts: float, start: float, period: float, count: int) -> int:
    if period <= 0:
        return 0
    return min(count - 1, max(0, int((ts - start) // period)))


def _episode_period_ts(episode: Mapping[str, Any]) -> tuple[float, bool]:
    """``(the timestamp this episode is bucketed on, is_open)``.

    A resolved episode belongs to the period it *ended* in — that is when it
    got over the line (or didn't).  An open one has no such moment, so it
    belongs to the period it stalled in: "still stalled" is a fact about the
    day the queue stopped, not about today.
    """
    if episode.get("resolved"):
        return float(episode.get("resolved_at") or 0.0), False
    return float(episode.get("entered_at") or 0.0), True


def fold_queue_outcomes(
    episodes: Iterable[Mapping[str, Any]],
    window: tuple[float, float],
    *,
    period_seconds: float | None = None,
    merged: Iterable[tuple[str, float]] = (),
    generated_at: float | None = None,
    log_location: Mapping[str, Any] | None = None,
    log_starts_at: float | None = None,
    extra_notes: Sequence[str] = (),
) -> ReportResult:
    """Fold block-log episodes (+ merge events) into outcome buckets.

    **Pure** — no log read, no DB, no daemon, no clock: *episodes* is whatever
    :func:`coord.block_log.episodes` returned, *merged* is a sequence of
    ``(key, ts)`` pairs for issues that merged in the window, and
    ``generated_at`` defaults to the window end so a frozen-clock test is
    deterministic.

    Every category comes out of :func:`coord.block_log.episode_category`, an
    **open vocabulary** read from the data — a cause this build has never seen
    appears in the report as itself.  Every bucket comes out of
    :func:`coord.block_log.episode_bucket`, and the ``by_design`` split out of
    :func:`coord.block_log.is_by_design`; none of the three is re-derived here,
    so the report and ``coord drive-queue block-log`` cannot drift.

    An episode that entered before the window and is **still open** is folded
    into the first period rather than dropped.  Dropping it would be the
    single most flattering bug available to this report: the longest-running
    unresolved stalls are exactly the ones whose ``entered_at`` has fallen off
    the back of the window.
    """
    from coord.block_log import (  # noqa: PLC0415
        AUTO_BUCKETS,
        BUCKET_OPEN,
        BUCKET_SUCCEEDED,
        OUTCOME_BUCKETS,
        UNCLASSIFIED_CATEGORY,
        episode_bucket,
        episode_category,
        is_by_design,
    )

    start, end = float(window[0]), float(window[1])
    period = float(period_seconds) if period_seconds else max(1.0, end - start)
    period_starts = _period_bounds(start, end, period)
    n_periods = len(period_starts)

    # (period, bucket, category, by_design) -> [key, ...]
    tally: dict[tuple[int, str, str, bool], list[str]] = {}
    windowed_keys: set[str] = set()
    open_before_window = 0

    for episode in episodes:
        key = str(episode.get("key") or "")
        ts, is_open = _episode_period_ts(episode)
        if is_open:
            if ts >= end:
                continue  # stalled after this window closed
            if ts < start:
                open_before_window += 1
        elif not (start <= ts < end):
            continue
        idx = _period_index(max(ts, start), start, period, n_periods)
        cell = (
            idx,
            episode_bucket(episode),
            episode_category(episode),
            is_by_design(episode),
        )
        tally.setdefault(cell, []).append(key)
        windowed_keys.add(key)

    for key, ts in merged:
        key = str(key)
        ts = float(ts)
        if not (start <= ts < end):
            continue
        if key in windowed_keys:
            # It stalled first. That episode already counted it in an
            # auto_resolved/human bucket; counting the merge again would
            # inflate the numerator with the very entries that needed help.
            continue
        idx = _period_index(ts, start, period, n_periods)
        tally.setdefault((idx, BUCKET_SUCCEEDED, "merged", False), []).append(key)

    bucket_order = {name: i for i, name in enumerate(OUTCOME_BUCKETS)}
    per_period_total: dict[int, int] = {}
    for (idx, _bucket, _cat, _bd), keys in tally.items():
        per_period_total[idx] = per_period_total.get(idx, 0) + len(keys)

    rows: list[dict[str, Any]] = []
    for (idx, bucket, category, by_design), keys in tally.items():
        total = per_period_total.get(idx, 0)
        rows.append(
            {
                "period_start": period_starts[idx],
                "period_end": period_starts[idx] + period,
                "bucket": bucket,
                "category": category,
                "by_design": by_design,
                "count": len(keys),
                "share_pct": round(100.0 * len(keys) / total, 1) if total else 0.0,
                "issues": sorted(set(keys)),
            }
        )
    rows.sort(
        key=lambda r: (
            r["period_start"],
            bucket_order.get(r["bucket"], len(bucket_order)),
            -r["count"],
            r["category"],
        )
    )

    grand_total = sum(per_period_total.values())
    grand_auto = sum(r["count"] for r in rows if r["bucket"] in AUTO_BUCKETS)
    grand_by_design = sum(r["count"] for r in rows if r["by_design"])

    notes: list[str] = list(extra_notes)
    notes.extend(_queue_outcomes_location_notes(log_location))
    if not rows:
        notes.append(
            "No queue entry reached a terminal state in this window — neither "
            "a recorded stall nor a merge. That is an EMPTY result, not a "
            "100% score."
        )
    else:
        notes.append(
            "headline: "
            + _headline_note(grand_auto, grand_total, grand_by_design)
            + " over the whole window."
        )
        if n_periods > 1:
            for idx, period_start in enumerate(period_starts):
                total = per_period_total.get(idx, 0)
                auto = sum(
                    r["count"]
                    for r in rows
                    if r["period_start"] == period_start and r["bucket"] in AUTO_BUCKETS
                )
                by_design = sum(
                    r["count"]
                    for r in rows
                    if r["period_start"] == period_start and r["by_design"]
                )
                notes.append(
                    f"  {_iso(period_start)}: "
                    + _headline_note(auto, total, by_design)
                )
    notes.extend(
        _queue_outcomes_caveats(
            rows,
            open_before_window=open_before_window,
            unclassified_label=UNCLASSIFIED_CATEGORY,
            open_bucket=BUCKET_OPEN,
        )
    )
    if rows and log_starts_at is not None and log_starts_at > start:
        # The recorder (#2235) landed in v0.5.90 and the fleet was on v0.5.88
        # when this report was written, so EVERY early window will hit this.
        # It is the same failure as a missing log, one granularity down: with
        # no stall records but a complete merge history, a period scores 100%
        # because nothing was measured — the most flattering possible way to
        # read an instrument that was switched off.
        notes.append(
            "PARTIAL WINDOW: the block log's oldest record is "
            f"{_iso(log_starts_at)}, after this window opened at "
            f"{_iso(start)}. Every period before that has merges but NO stall "
            "records, so its score is unmeasured, not perfect — the recorder "
            "was not running yet. Trust the periods from "
            f"{_iso(log_starts_at)} onward."
        )

    totals = (
        {"count": grand_total, "share_pct": 100.0 if grand_total else 0.0}
        if rows
        else None
    )

    return ReportResult(
        report_id="queue-outcomes",
        generated_at=end if generated_at is None else float(generated_at),
        window=(start, end),
        columns=list(QUEUE_OUTCOMES_COLUMNS),
        column_meta=list(QUEUE_OUTCOMES_COLUMN_META),
        rows=rows,
        notes=notes,
        totals=totals,
        chart=queue_outcomes_chart(rows, n_periods),
    )


def queue_outcomes_chart(
    rows: Sequence[Mapping[str, Any]], n_periods: int
) -> ChartSpec | None:
    """The chart declaration for a ``queue-outcomes`` fold (#2271).

    Exactly the two views the report's own description already promises:
    ``24h`` is a single period, so it is **one stacked bar per bucket** over
    the categories in it; ``7d``/``4w`` are the same arithmetic in 7 daily /
    4 weekly points, so they are **one trendline per bucket** over
    ``period_start``.  Both derive from ``count`` — the column the table
    renders — so there is nothing to keep in sync.

    ``None`` when there is nothing to plot: an empty fold is an EMPTY result,
    and an axis with no marks on it reads as a zero score rather than as no
    measurement.
    """
    if not rows:
        return None
    if n_periods > 1:
        return ChartSpec(
            kind="line",
            series=(ChartSeries(label="Entries", column="count"),),
            x="period_start",
            group_by="bucket",
            title="Outcomes per period",
            y_label="Entries",
        )
    return ChartSpec(
        kind="bar",
        series=(ChartSeries(label="Entries", column="count"),),
        x="category",
        group_by="bucket",
        stacked=True,
        title="Outcomes by bucket",
        y_label="Entries",
    )


def _headline_note(auto: int, total: int, by_design: int) -> str:
    """``(succeeded + auto_*) / total``, with the by-design-excluded variant.

    Both, always, because they answer different questions: the raw fraction is
    the operator's stated target, and the adjusted one is the only fraction
    that CAN reach 100% — a Gate-A sign-off and a policy refusal are supposed
    to stop for a human, so counting them as misses would make a working queue
    read as permanent failure (#2270).
    """
    if total <= 0:
        return "no terminal entries"
    pct = 100.0 * auto / total
    out = f"{pct:.1f}% got over the line without a human ({auto}/{total})"
    remaining = total - by_design
    if by_design:
        adjusted = (100.0 * auto / remaining) if remaining > 0 else 100.0
        out += (
            f" · excluding {by_design} that stop for a human BY DESIGN "
            f"(Gate A, policy): {adjusted:.1f}% ({auto}/{remaining})"
        )
    return out


def _queue_outcomes_location_notes(
    location: Mapping[str, Any] | None,
) -> list[str]:
    """Say where the log was read — and shout when it was not there (#1806).

    The block log is a per-host file and only the host that runs the tick
    writes one, so a reader that quietly reports zeros from the wrong machine
    has produced a *perfect score* out of a missing file.  That is the exact
    thin-client trap #1806 documents, and the one thing this report must never
    do silently.
    """
    if not location:
        return []
    path = location.get("path") or "?"
    host = location.get("host") or "?"
    if location.get("exists"):
        return [f"source: the block log on {host} ({path})."]
    return [
        f"NO BLOCK LOG ON THIS HOST: {path} does not exist on {host}, so this "
        "report has no input and the table above is EMPTY — not a clean "
        "sweep. The log is written by the drive-queue tick and is per-host "
        "(#2235), so run this on the tick host, or point a board_service "
        "thin client at that host's daemon and let it answer.",
    ]


def _queue_outcomes_caveats(
    rows: Sequence[Mapping[str, Any]],
    *,
    open_before_window: int,
    unclassified_label: str,
    open_bucket: str,
) -> list[str]:
    from coord.block_log import BUCKET_AUTO_RESCUE  # noqa: PLC0415

    notes: list[str] = []
    if not rows:
        return notes
    if not any(r["bucket"] == BUCKET_AUTO_RESCUE for r in rows):
        notes.append(
            f"`{BUCKET_AUTO_RESCUE}` is 0 because nothing writes it yet — the "
            "rescue agent (#2268) does not exist. It is modelled as its own "
            "series from day one so this report does not change shape when it "
            "lands, and so 'a deterministic arm fixed it' never quietly "
            "becomes 'an agent judged it'."
        )
    unclassified = sum(
        r["count"] for r in rows if r["category"] == unclassified_label
    )
    if unclassified:
        notes.append(
            f"{unclassified} episode(s) have no cause at all and are grouped "
            f"as '{unclassified_label}' — a stall nobody has diagnosed. Run "
            "`coord drive-queue diagnose` (#2276) to fill that column; until "
            "then the category breakdown under-reports every real cause."
        )
    still_open = sum(r["count"] for r in rows if r["bucket"] == open_bucket)
    if still_open:
        notes.append(
            f"{still_open} entr(y/ies) are still stalled. Read this beside the "
            "headline: a queue that stops needing interventions by leaving "
            "everything blocked forever scores well on `human` and badly here."
        )
    if open_before_window:
        notes.append(
            f"{open_before_window} of those stalled before this window opened "
            "and are folded into its first period — they are counted, not "
            "dropped, because the oldest unresolved stalls are exactly the "
            "ones a window would otherwise hide."
        )
    return notes


def _default_block_log_episodes() -> list[dict]:
    """Every episode in this host's block log, oldest first.

    The WHOLE log, never a windowed read: :func:`coord.block_log.read_events`
    filters raw records, and an episode whose `enter` fell before the window
    would arrive as an orphan `resolve` that
    :func:`coord.block_log.episodes` correctly drops — silently deleting
    exactly the long stalls this report exists to count.  Windowing happens on
    the paired episode, in :func:`fold_queue_outcomes`.  A whole-file parse is
    the shape that module already commits to (it rotates at 4 MiB for this
    reason).
    """
    from coord.block_log import episodes, read_events  # noqa: PLC0415

    return episodes(read_events())


def _fetch_merged_keys(
    *,
    since: float,
    until: float,
    repo: str | None,
    fetch: Callable[..., Mapping[str, Any]],
) -> tuple[list[tuple[str, float]], list[str]]:
    """``merged`` audit events in the window, as ``[(key, ts)]`` + notes.

    Fails **soft**: a report whose `succeeded` count is missing under-states
    the headline (never over-states it), so a broken audit read is worth a
    loud note rather than a dead report.
    """
    try:
        entries, truncated = fetch_audit_window(
            since=since,
            until=until,
            repo=repo,
            fetch=fetch,
            category=_MERGE_AUDIT_CATEGORY,
            event_type=_MERGE_AUDIT_EVENT,
        )
    except Exception as exc:  # noqa: BLE001 — surfaced as a note, not a crash
        return [], [
            "WARNING: the audit trail could not be read "
            f"({type(exc).__name__}: {exc}) — the `succeeded` bucket (merged "
            "with no stall) is MISSING from this run, so the headline is a "
            "lower bound, not the real number."
        ]

    out: list[tuple[str, float]] = []
    for entry in entries:
        if entry.get("event_type") != _MERGE_AUDIT_EVENT:
            continue
        repo_name, issue = entry.get("repo"), entry.get("issue")
        if not repo_name or issue is None:
            continue
        out.append((f"{repo_name}#{int(issue)}", float(entry.get("ts") or 0.0)))
    notes: list[str] = []
    if truncated:
        notes.append(
            "TRUNCATED: the audit trail's merge events could not be fully "
            "fetched for this window, so the `succeeded` bucket is a lower "
            "bound. Use a shorter window for a complete answer."
        )
    return out, notes


def run_queue_outcomes(
    *,
    window: str = "24h",
    until: str = "",
    repo: str = "",
    now: float | None = None,
    episode_source: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    fetch: Callable[..., Mapping[str, Any]] | None = None,
    location: Mapping[str, Any] | None = None,
) -> ReportResult:
    """Read this host's block log (+ the merge events) and fold it.

    ``now``/``episode_source``/``fetch``/``location`` are test seams; the
    report's own parameters are ``window``/``until``/``repo``.

    Refuses to invent a score when the log is not here: on a host with no
    ``queue-block-log.jsonl`` this returns an EMPTY result — columns intact,
    zero rows — with a note naming the host and the path, rather than a table
    of zeros that reads as a perfect week (#1806, and this issue's own
    acceptance).
    """
    from coord.block_log import log_location as _log_location  # noqa: PLC0415

    generated_at = time.time() if now is None else float(now)
    end = parse_timestamp(until) if until else generated_at
    start, end, period = resolve_queue_outcomes_window(window, end)

    where = dict(_log_location() if location is None else location)
    if not where.get("exists"):
        return fold_queue_outcomes(
            (), (start, end), period_seconds=period,
            generated_at=generated_at, log_location=where,
        )

    source = _default_block_log_episodes if episode_source is None else episode_source
    episodes = list(source() or [])
    # Before the repo filter: "when did this log start recording?" is a fact
    # about the FILE, and a repo that happens to have stalled late must not
    # make the whole log look younger than it is.
    entered = [float(ep.get("entered_at") or 0.0) for ep in episodes]
    log_starts_at = min((t for t in entered if t > 0), default=None)
    if repo:
        episodes = [
            ep for ep in episodes
            if str(ep.get("key") or "").split("#")[0] == repo
        ]

    merged, notes = _fetch_merged_keys(
        since=start,
        until=end,
        repo=repo or None,
        fetch=_default_fetch if fetch is None else fetch,
    )

    return fold_queue_outcomes(
        episodes,
        (start, end),
        period_seconds=period,
        merged=merged,
        generated_at=generated_at,
        log_location=where,
        log_starts_at=log_starts_at,
        extra_notes=notes,
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


DRIVE_QUEUE_STATUS = ReportDef(
    id="drive-queue-status",
    title="Drive Queue Status",
    description=(
        "A live snapshot of the drive queue — one row per queued entry in "
        "run order, with its state, machine pin, attempts/deferrals and the "
        "tick's own last_reason. A snapshot, not a history: `drive_queue` "
        "has no `completed_at`, so this shows what is queued now, not what "
        "the queue has processed."
    ),
    params=(
        ReportParam(
            id="repo",
            label="Repo",
            kind="text",
            default="",
            help="Restrict to one repo by name. Empty means all repos.",
        ),
    ),
    run=run_drive_queue_status,
)


USAGE = ReportDef(
    id="usage",
    title="Usage",
    description=(
        "Cost and token spend for a time window, one row per issue (or per "
        "repo): legs, tokens in/out, captured $, estimated ~$ for legs with "
        "no captured cost, and the total. Estimates use the daemon's own "
        "loaded `pricing:` block, so they agree with `coord usage` by "
        "construction."
    ),
    params=(
        ReportParam(
            id="window",
            label="Time window",
            kind="choice",
            choices=USAGE_WINDOW_CHOICES,
            default="today",
            help=(
                "today/week/month are local calendar periods; 7d/30d are "
                "rolling windows ending now."
            ),
        ),
        ReportParam(
            id="group_by",
            label="Group by",
            kind="choice",
            choices=USAGE_GROUP_BY_CHOICES,
            default="issue",
            help="One row per issue, or one row per repo.",
        ),
        ReportParam(
            id="repo",
            label="Repo",
            kind="text",
            default="",
            help="Restrict to one repo by name. Empty means all repos.",
        ),
    ),
    run=run_usage,
)


QUEUE_OUTCOMES = ReportDef(
    id="queue-outcomes",
    title="Queue Outcomes",
    description=(
        "What fraction of the queue got over the line without a human. Every "
        "entry that reached a terminal state in the window, bucketed as "
        "succeeded / auto_resolved_mechanism / auto_resolved_rescue / human / "
        "open, with the human bucket broken down by cause and split again by "
        "`by_design` (a Gate-A sign-off and a policy refusal are SUPPOSED to "
        "stop for a person). Folded from the drive-queue block log (#2235), "
        "which is per-host — run it where the tick runs, or let that host's "
        "daemon answer. `24h` is one bar per category; `7d`/`4w` are the same "
        "arithmetic in 7 daily / 4 weekly periods, so a client trends it by "
        "grouping rows on `period_start`."
    ),
    params=(
        ReportParam(
            id="window",
            label="Window",
            kind="choice",
            choices=QUEUE_OUTCOMES_WINDOW_CHOICES,
            default="24h",
            help=(
                "Span and bucket size together: 24h is a single period, 7d is "
                "7 daily periods, 4w is 4 weekly ones. Periods are aligned to "
                "`until`, not to the civil calendar."
            ),
        ),
        # Same name, same semantics and the same validator as
        # `issue-activity`'s (#2270: follow the existing convention rather
        # than inventing one). `since` is deliberately absent — `window` sets
        # the span, and two ways to say it would let them disagree.
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
    run=run_queue_outcomes,
)


# ── CSV serialisation (#1765) ──────────────────────────────────────────────
#
# One serializer, server-side, for every surface: `coord report run --format
# csv`, `GET /report/{id}?format=csv`, and the coord-tui Reports panel's
# Export action (which fetches the route rather than formatting anything
# itself).  Doing it here is not incidental — the values on the wire are
# **raw** (`started_at` is an epoch float, `machines` is a list), and every
# renderer turns those into display strings (`13h ago`, `dellserver,
# precision`).  A client-side CSV would therefore export the *formatting*,
# not the data: an epoch would become a relative string no spreadsheet can
# sort, and the bytes would silently depend on when Export was clicked.
#
# Line terminator is `\n`, not RFC 4180's `\r\n`: this is a Unix tool whose
# output is piped and redirected, `csv.reader` accepts either, and every
# spreadsheet we care about does too.  Fixing it (rather than taking
# `csv.writer`'s platform-ish default) is what makes CLI and daemon bytes
# identical.
_CSV_LINE_TERMINATOR = "\n"


def _csv_scalar(value: Any) -> str:
    """One *raw* value → its CSV text.  Never a display string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        # Before the int check — bool is an int in Python, and `true`/`false`
        # is what every consumer of this file expects to see.
        return "true" if value else "false"
    return str(value)


def _csv_cell(value: Any) -> str:
    """One row value → one CSV field.

    Composite values collapse into a single field rather than spilling into
    extra columns: lists (``machines``, ``test_verdicts``) join with ``"; "``,
    and dicts (``drive_exit``) render as ``key=value`` pairs joined the same
    way.  ``drive_exit.reason`` is embedded **verbatim** — commas, quotes and
    newlines and all — because `csv.writer` quotes and escapes it, and a
    round-trip through `csv.reader` has to return the original text (#1631's
    multi-line driver-exit reason is the regression fixture).  JSON-encoding
    the dict would have escaped that newline into a literal ``\\n`` and lost
    the round-trip.
    """
    if isinstance(value, (list, tuple)):
        return "; ".join(_csv_scalar(v) for v in value)
    if isinstance(value, Mapping):
        return "; ".join(f"{k}={_csv_scalar(v)}" for k, v in value.items())
    return _csv_scalar(value)


def _csv_comment(text: str) -> list[str]:
    """A note → its ``#``-prefixed line(s).  A note that itself spans lines
    gets one ``#`` per physical line, so no fragment can escape into the
    data and be parsed as a row."""
    lines = str(text).splitlines() or [""]
    return [f"# {line}" if line else "#" for line in lines]


def result_to_csv(result: "ReportResult | Mapping[str, Any]") -> str:
    """Serialise a :class:`ReportResult` (or its ``to_dict()`` form) as CSV.

    Shape:

    * leading ``#``-prefixed comment lines — the report id, the window, and
      **every** ``notes`` entry.  Notes are the derived anomalies and are the
      most valuable part of ``issue-activity``; they are not rows, and they
      must never silently vanish, so they ride along as comments that keep
      the file self-describing and still let it parse once ``#`` lines are
      skipped.
    * a header row, labelled from ``column_meta[].label`` (#1760) when
      present and from the raw column key otherwise.
    * one row per ``rows`` entry, raw values only.
    * ``totals`` (#1763), when the report has one, as a final row — flagged
      in the comments so nobody mistakes it for another data row.  Reports
      without a meaningful sum emit no such row and are unaffected.
    """
    data = result.to_dict() if isinstance(result, ReportResult) else dict(result)

    columns = [str(c) for c in (data.get("columns") or [])]
    labels = {
        str(m.get("id")): str(m.get("label") or m.get("id"))
        for m in (data.get("column_meta") or [])
        if isinstance(m, Mapping)
    }
    window = data.get("window") or [None, None]

    comments: list[str] = [
        f"# report: {data.get('report_id')}",
        f"# window: {_iso(window[0])} to {_iso(window[1])}",
        f"# generated: {_iso(data.get('generated_at'))}",
    ]
    rows = list(data.get("rows") or [])
    comments.append(f"# rows: {len(rows)}")
    # The export is the report's own canonical row order — never a client's
    # transient sort (#1762), which is view state over one result set.
    comments.append("# order: the report's canonical row order")
    totals = data.get("totals")
    if isinstance(totals, Mapping):
        comments.append(
            "# totals: the final row is the grand total, not a data row "
            "(identity columns are blank)"
        )
    for note in data.get("notes") or []:
        comments.extend(_csv_comment(note))

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator=_CSV_LINE_TERMINATOR)
    writer.writerow([labels.get(c, c) for c in columns])
    for row in rows:
        row = row if isinstance(row, Mapping) else {}
        writer.writerow([_csv_cell(row.get(c)) for c in columns])
    if isinstance(totals, Mapping):
        writer.writerow([_csv_cell(totals.get(c)) for c in columns])

    header = "".join(line + _CSV_LINE_TERMINATOR for line in comments)
    return header + buf.getvalue()


def csv_filename(result: "ReportResult | Mapping[str, Any]") -> str:
    """``issue-activity-20260804-1130.csv`` — the suggested download name.

    Derived from the *result* (its window end), not from the wall clock, so
    the daemon's ``Content-Disposition`` and the panel's save-dialog
    suggestion agree for the same run.
    """
    data = result.to_dict() if isinstance(result, ReportResult) else dict(result)
    window = data.get("window") or [None, None]
    stamp_at = window[1] if window[1] is not None else data.get("generated_at")
    try:
        stamp = datetime.fromtimestamp(float(stamp_at), tz=timezone.utc).strftime(
            "%Y%m%d-%H%M"
        )
    except (TypeError, ValueError):
        stamp = "unknown"
    report_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(data.get("report_id") or "report"))
    return f"{report_id}-{stamp}.csv"


REPORTS: dict[str, ReportDef] = {
    ISSUE_ACTIVITY.id: ISSUE_ACTIVITY,
    DRIVE_QUEUE_STATUS.id: DRIVE_QUEUE_STATUS,
    USAGE.id: USAGE,
    QUEUE_OUTCOMES.id: QUEUE_OUTCOMES,
}


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
