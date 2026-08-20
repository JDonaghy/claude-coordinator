"""Forge/CI availability instrumentation — Phase 0 of the Forge Independence
program (#1896).

Phases 3-6 of that program (issue-store split, parentage, message-bus
cutover) are 20-40 issues of work whose entire justification is "our forge
is unreliable enough that leaving it pays for itself". Whether that premise
is even true has never been measured — every data point that exists lives
in a chat transcript about one bad day (2026-08-06's 7+-hour GitHub Actions
outage). This module turns that into durable data, cheaply, by observing
three seams coord already touches on every relevant call, so nothing here
adds a single extra network round trip:

1. **CI reads** — :func:`record_ci_check_fetch`, called from
   :meth:`coord.ci_github.GitHubCi._fetch` on every live (non-cached)
   ``gh pr checks`` read.
2. **Forge API calls** — :func:`record_gh_call`, called from
   :func:`coord.github_ops._gh`, the single seam all 71 public functions in
   that module funnel through (#1483).
3. **Merge-gate refusals** — :func:`record_merge_gate_refusal`, called from
   :func:`coord.merge_queue.process` for each live (never dry-run)
   ``checks_failed``/``checks_pending``/``checks_stale`` :class:`~coord.
   merge_queue.MergeEvent`.

Storage deliberately follows the audit-trail grain (#1041/milestone 33)
rather than inventing a parallel log/table: every observation is one row in
the existing ``audit_log`` (via :func:`coord.audit.record_audit`), tagged
``category="forge_availability"``. :func:`availability_report` is the read
side — ``coord diagnose --forge-availability``.

**Best-effort, unconditionally.** Every ``record_*`` function here is a thin
wrapper that can never raise, retry, or delay its caller — ``record_audit``
itself already swallows all exceptions (see its docstring), and each
function below adds its own belt-and-suspenders ``try/except`` on top so
that guarantee holds even if this module's own bookkeeping (the periodic
prune sweep) misbehaves. Measurement must never become a new way for the
forge's actual unreliability to take coord down with it.

This is measurement only — see the issue for why acting on the data
(pausing the drive queue when the forge is degraded) is explicitly a
different, sibling issue (#1893).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from coord.audit import MAX_LIMIT as _AUDIT_MAX_LIMIT
from coord.audit import query_audit_log, record_audit

_log = logging.getLogger(__name__)

CATEGORY = "forge_availability"

EVENT_GH_CALL = "gh_call"
EVENT_CI_CHECK_FETCH = "ci_check_fetch"
EVENT_MERGE_GATE_REFUSAL = "merge_gate_refusal"

# gh_call / ci_check_fetch outcomes that count as the forge being reachable.
# "app_error" is gh running fine and reporting a normal application-level
# failure (e.g. "label not found") -- that is NOT a forge-availability
# problem, it is business as usual, so it counts as available. "transient"
# (an error string matching github_ops._is_transient_error -- auth,
# rate-limit, network) and "unreachable" (gh missing / timed out / OSError,
# or a raised read failure on the CI seam) are the two outcomes that
# actually say something about forge/CI availability.
_AVAILABLE_OUTCOMES = frozenset({"ok", "app_error"})

# Refusal reasons this issue asks to be tracked (#1896 scope: "checks_failed
# / checks_pending / checks_stale are already distinct MergeEvent kinds ...
# persist the counts"). Deliberately narrower than every MergeEvent kind
# that can block a merge (e.g. checks_absent, checks_unreadable, conflict)
# -- those are real refusal reasons too, but out of THIS issue's stated
# scope; widening the set later is additive, not a migration.
MERGE_GATE_REFUSAL_KINDS = frozenset({"checks_failed", "checks_pending", "checks_stale"})

# How often (seconds, per-process) the opportunistic retention sweep runs
# after a write. A DELETE scan on every single observation -- the whole
# point of this module is to be cheap enough to fire on every `gh` call --
# would defeat that; observations are cheap to lose track of for an hour
# without threatening the "does not grow unboundedly" acceptance bar.
_PRUNE_INTERVAL_S = 3600.0

# Retention window (days). ">= 90 days" per the issue's acceptance bar.
RETENTION_DAYS = 90.0

_last_prune_at = 0.0


def record_gh_call(
    argv: tuple[str, ...], *, outcome: str, duration_s: float, detail: str = "",
) -> None:
    """Best-effort: one row per :func:`coord.github_ops._gh` invocation.

    ``outcome`` is one of ``"ok"`` (exit 0), ``"app_error"`` (non-zero exit,
    not an auth/network/rate-limit failure -- an ordinary application-level
    error), ``"transient"`` (non-zero exit matching
    ``github_ops._is_transient_error`` -- auth, rate-limit, network), or
    ``"unreachable"`` (the ``gh`` binary was missing, the call timed out, or
    raised some other ``OSError`` before it could even run).
    """
    _safe_record(
        event_type=EVENT_GH_CALL,
        summary=f"gh {argv[0] if argv else '(no args)'}: {outcome}",
        details={
            "argv0": argv[0] if argv else "",
            "outcome": outcome,
            "duration_s": round(duration_s, 3),
            "detail": detail[:200] if detail else "",
        },
    )


def record_ci_check_fetch(
    repo: str,
    number: int,
    *,
    outcome: str,
    duration_s: float,
    conclusions: dict[str, int] | None = None,
    detail: str = "",
) -> None:
    """Best-effort: one row per live (cache-miss) ``gh pr checks`` read.

    ``outcome`` is ``"ok"`` or ``"unreachable"``. ``conclusions`` is the
    check-level conclusion distribution (e.g. ``{"success": 3, "failure":
    1}``) when ``outcome == "ok"`` -- the "check-level conclusion
    distribution" the issue asks for, alongside reachability.
    """
    _safe_record(
        event_type=EVENT_CI_CHECK_FETCH,
        summary=f"{repo}#{number}: CI checks {outcome}",
        repo=repo,
        issue=number,
        details={
            "outcome": outcome,
            "duration_s": round(duration_s, 3),
            "conclusions": conclusions or {},
            "detail": detail[:200] if detail else "",
        },
    )


def record_merge_gate_refusal(
    *, repo: str, issue: int | None, reason: str, message: str,
) -> None:
    """Best-effort: one row per live merge-gate CI refusal.

    Only ``reason in MERGE_GATE_REFUSAL_KINDS`` (see that constant) should be
    passed here -- callers filter before calling, this function does not
    re-filter, so it stays a plain unconditional recorder like its siblings.
    """
    _safe_record(
        event_type=EVENT_MERGE_GATE_REFUSAL,
        summary=f"{repo}#{issue}: merge blocked ({reason})",
        repo=repo,
        issue=issue,
        details={"reason": reason, "message": message[:300] if message else ""},
    )


def _safe_record(
    *,
    event_type: str,
    summary: str,
    details: dict[str, Any],
    repo: str | None = None,
    issue: int | None = None,
) -> None:
    try:
        record_audit(
            tier="operational",
            category=CATEGORY,
            event_type=event_type,
            actor="system",
            summary=summary,
            repo=repo,
            issue=issue,
            details=details,
        )
        _maybe_prune()
    except Exception as exc:  # noqa: BLE001 -- measurement must never affect the caller
        _log.debug("forge_availability: best-effort record failed: %s", exc)


def _maybe_prune(*, force: bool = False) -> None:
    """Delete ``forge_availability`` rows older than :data:`RETENTION_DAYS`.

    Throttled to once per :data:`_PRUNE_INTERVAL_S` per process (``force``
    bypasses the throttle, for tests) -- see the module docstring for why a
    DELETE scan on every single write would defeat the point of this module
    being cheap enough to fire on every ``gh`` call.
    """
    global _last_prune_at
    now = time.time()
    if not force and (now - _last_prune_at) < _PRUNE_INTERVAL_S:
        return
    _last_prune_at = now
    try:
        from coord.db import get_connection  # noqa: PLC0415

        cutoff = now - RETENTION_DAYS * 86400.0
        conn = get_connection()
        conn.execute(
            "DELETE FROM audit_log WHERE category = ? AND ts < ?",
            (CATEGORY, cutoff),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 -- best-effort, never blocks a caller
        _log.debug("forge_availability: prune sweep failed: %s", exc)


# ── Read side: the `coord diagnose --forge-availability` read-out ──────────


class AvailabilityReport:
    """Summary of forge/CI availability over a trailing window.

    ``uptime_pct`` and ``longest_unavailable_stretch_s`` are computed over
    the merged, time-ordered sequence of ``gh_call`` + ``ci_check_fetch``
    observations -- this is an *observation-based* signal, not a continuous
    heartbeat: a stretch with no forge calls at all (nights, a quiet repo)
    is not a gap in availability, it's a gap in *observations*, and
    "longest unavailable stretch" only measures contiguous runs of
    observations that came back unavailable, not wall-clock silence.
    """

    def __init__(
        self,
        *,
        window_days: float,
        since: float,
        until: float,
        gh_calls: int,
        ci_fetches: int,
        available: int,
        unavailable: int,
        longest_unavailable_stretch_s: float,
        refusals_by_reason: dict[str, int],
        truncated: bool,
    ) -> None:
        self.window_days = window_days
        self.since = since
        self.until = until
        self.gh_calls = gh_calls
        self.ci_fetches = ci_fetches
        self.available = available
        self.unavailable = unavailable
        self.longest_unavailable_stretch_s = longest_unavailable_stretch_s
        self.refusals_by_reason = refusals_by_reason
        self.truncated = truncated

    @property
    def total_observations(self) -> int:
        return self.available + self.unavailable

    @property
    def uptime_pct(self) -> float | None:
        if self.total_observations == 0:
            return None
        return 100.0 * self.available / self.total_observations

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            "since": self.since,
            "until": self.until,
            "gh_calls": self.gh_calls,
            "ci_fetches": self.ci_fetches,
            "total_observations": self.total_observations,
            "available": self.available,
            "unavailable": self.unavailable,
            "uptime_pct": self.uptime_pct,
            "longest_unavailable_stretch_s": self.longest_unavailable_stretch_s,
            "refusals_by_reason": dict(self.refusals_by_reason),
            "truncated": self.truncated,
        }


# Safety cap on how many audit rows a single report will page through --
# bounds worst case cost the same way `coord.audit.query_audit_log`'s own
# MAX_LIMIT bounds a single page; a window with more observations than this
# reports `truncated=True` rather than paging forever.
_MAX_REPORT_ROWS = 20_000


def availability_report(
    *, window_days: float = 30.0, now: float | None = None,
) -> AvailabilityReport:
    """Summarize forge/CI availability over the trailing *window_days*.

    Read-only; queries the local ``audit_log`` via
    :func:`coord.audit.query_audit_log`, paginating until the window is
    exhausted or :data:`_MAX_REPORT_ROWS` is hit (``truncated=True`` in the
    latter case, reported rather than hidden).
    """
    now = now if now is not None else time.time()
    since = now - window_days * 86400.0

    entries: list[dict[str, Any]] = []
    cursor: str | None = None
    truncated = False
    while True:
        page = query_audit_log(
            since=since, until=now, category=CATEGORY,
            limit=_AUDIT_MAX_LIMIT, cursor=cursor,
        )
        entries.extend(page["entries"])
        if len(entries) >= _MAX_REPORT_ROWS:
            truncated = bool(page["has_more"])
            break
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]

    observations = [
        e for e in entries if e["event_type"] in (EVENT_GH_CALL, EVENT_CI_CHECK_FETCH)
    ]
    # query_audit_log returns newest-first; availability math wants
    # chronological order so "contiguous" means "contiguous in time".
    observations.sort(key=lambda e: (e["ts"], e["id"]))

    gh_calls = sum(1 for e in observations if e["event_type"] == EVENT_GH_CALL)
    ci_fetches = sum(1 for e in observations if e["event_type"] == EVENT_CI_CHECK_FETCH)

    available = 0
    unavailable = 0
    longest_stretch = 0.0
    run_start_ts: float | None = None
    run_end_ts: float | None = None
    for e in observations:
        details = e.get("details") or {}
        outcome = details.get("outcome")
        duration_s = details.get("duration_s") or 0.0
        is_available = outcome in _AVAILABLE_OUTCOMES
        if is_available:
            available += 1
            if run_start_ts is not None:
                longest_stretch = max(longest_stretch, (run_end_ts or run_start_ts) - run_start_ts)
            run_start_ts = None
            run_end_ts = None
        else:
            unavailable += 1
            if run_start_ts is None:
                run_start_ts = e["ts"]
            run_end_ts = e["ts"] + duration_s
    if run_start_ts is not None:
        longest_stretch = max(longest_stretch, (run_end_ts or run_start_ts) - run_start_ts)

    refusals_by_reason: dict[str, int] = {}
    for e in entries:
        if e["event_type"] != EVENT_MERGE_GATE_REFUSAL:
            continue
        reason = (e.get("details") or {}).get("reason", "unknown")
        refusals_by_reason[reason] = refusals_by_reason.get(reason, 0) + 1

    return AvailabilityReport(
        window_days=window_days,
        since=since,
        until=now,
        gh_calls=gh_calls,
        ci_fetches=ci_fetches,
        available=available,
        unavailable=unavailable,
        longest_unavailable_stretch_s=longest_stretch,
        refusals_by_reason=refusals_by_reason,
        truncated=truncated,
    )


def _format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "0s"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def format_report_lines(report: AvailabilityReport) -> list[str]:
    """Human-readable report lines for ``coord diagnose --forge-availability``."""
    lines: list[str] = []
    uptime = report.uptime_pct
    if uptime is None:
        lines.append(
            f"no forge/CI observations in the trailing {report.window_days:.0f}d "
            "(nothing has called `gh` or read CI checks yet in this window)"
        )
    else:
        lines.append(
            f"uptime: {uptime:.2f}% ({report.available}/{report.total_observations} "
            f"observations available) over the trailing {report.window_days:.0f}d"
        )
        lines.append(
            f"observations: {report.gh_calls} gh call(s), {report.ci_fetches} CI check-fetch(es)"
        )
        lines.append(
            f"longest unavailable stretch: {_format_duration(report.longest_unavailable_stretch_s)}"
        )
    if report.refusals_by_reason:
        lines.append("merge-gate refusals by reason:")
        for reason in sorted(report.refusals_by_reason):
            lines.append(f"  {reason}: {report.refusals_by_reason[reason]}")
    else:
        lines.append("merge-gate refusals by reason: none")
    if report.truncated:
        lines.append(
            f"⚠ truncated at {_MAX_REPORT_ROWS} rows -- narrow --window-days for exact figures"
        )
    return lines


def summary_line(report: AvailabilityReport) -> str:
    """Machine-parseable trailer line, same family as ``GRAPH_HEALTH:``."""
    uptime = report.uptime_pct
    uptime_str = f"{uptime:.2f}" if uptime is not None else "n/a"
    refusals_total = sum(report.refusals_by_reason.values())
    return (
        f"FORGE_AVAILABILITY: window_days={report.window_days:.0f} "
        f"observations={report.total_observations} uptime_pct={uptime_str} "
        f"longest_outage_s={report.longest_unavailable_stretch_s:.0f} "
        f"refusals_total={refusals_total} truncated={report.truncated}"
    )
