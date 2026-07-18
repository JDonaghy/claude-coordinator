"""Usage tracking: parse token/cost data from worker stream-json logs.

Provides per-assignment and per-model cost breakdowns, session burn rate,
and summary helpers for ``coord usage`` and the burn-rate warning in
``coord status``.

Usage data is collected from two sources:

* **Local logs** — ``~/.coord/logs/<assignment_id>.log`` (stream-json).
  These exist when the agent ran on the same machine as the coordinator.
* **Remote agent status** — HTTP ``/status`` on agent servers.
  The agent already reports ``cost_so_far`` / ``total_cost_usd`` in its
  ``list_assignments()`` response; we use those when a local log is absent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from coord.models import Assignment

# Re-export COORD_DIR so callers don't need to import state directly.
from coord.state import COORD_DIR

LOGS_DIR = COORD_DIR / "logs"

# Burn rate threshold ($/hr) above which coord status shows a warning line.
HIGH_BURN_RATE_USD_PER_HOUR = 2.0


# ── Data classes ─────────────────────────────────────────────────────────────


@dataclass
class AssignmentUsage:
    """Cost/usage data for a single assignment."""

    assignment_id: str
    repo_name: str
    issue_number: int
    issue_title: str
    status: str  # pending | running | done | failed
    model: str | None = None
    total_cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def duration_str(self) -> str:
        if self.duration_ms is None:
            return "?"
        s = self.duration_ms / 1000.0
        if s < 60:
            return f"{s:.0f}s"
        m, sec = divmod(int(s), 60)
        if m < 60:
            return f"{m}m {sec}s"
        h, m = divmod(m, 60)
        return f"{h}h {m}m"


@dataclass
class SessionUsage:
    """Aggregated usage across all assignments in the current session."""

    started_at: float | None = None  # Unix timestamp from session.json
    assignments: list[AssignmentUsage] = field(default_factory=list)

    @property
    def total_cost_usd(self) -> float:
        return sum(a.total_cost_usd for a in self.assignments)

    @property
    def total_input_tokens(self) -> int:
        return sum(a.input_tokens for a in self.assignments)

    @property
    def total_output_tokens(self) -> int:
        return sum(a.output_tokens for a in self.assignments)

    @property
    def elapsed_hours(self) -> float | None:
        """Hours since session start; None if no session timestamp."""
        if self.started_at is None:
            return None
        elapsed_sec = time.time() - self.started_at
        # Guard against negative/zero to avoid division weirdness.
        return max(elapsed_sec, 60.0) / 3600.0

    def burn_rate_usd_per_hour(self) -> float | None:
        """$/hr based on total cost and elapsed session time.

        Returns None if the session start time is unknown.
        """
        hours = self.elapsed_hours
        if hours is None:
            return None
        if self.total_cost_usd == 0.0:
            return 0.0
        return self.total_cost_usd / hours

    def cost_by_model(self) -> dict[str, float]:
        """Map model name → total cost across all assignments."""
        result: dict[str, float] = {}
        for a in self.assignments:
            key = a.model or "(unknown)"
            result[key] = result.get(key, 0.0) + a.total_cost_usd
        return result

    def count_by_model(self) -> dict[str, int]:
        """Map model name → number of assignments."""
        result: dict[str, int] = {}
        for a in self.assignments:
            key = a.model or "(unknown)"
            result[key] = result.get(key, 0) + 1
        return result


# ── Log parsing ───────────────────────────────────────────────────────────────


def parse_usage_from_log(log_path: Path) -> AssignmentUsage | None:
    """Parse an :class:`AssignmentUsage` from a stream-json log file.

    Returns ``None`` if the file doesn't exist, isn't stream-json, or can't
    be parsed.  The returned object has placeholder values for fields that
    aren't available from the log alone (``assignment_id``, ``repo_name``,
    etc.) — callers must fill those in from the board.
    """
    from coord.worker_events import is_stream_json, parse_log

    if not log_path.exists():
        return None
    if not is_stream_json(log_path):
        return None
    try:
        summary = parse_log(log_path, tail_bytes=0)
    except OSError:
        return None
    return AssignmentUsage(
        assignment_id="",  # caller fills in
        repo_name="",  # caller fills in
        issue_number=0,  # caller fills in
        issue_title="",  # caller fills in
        status="",  # caller fills in
        model=summary.model_used,
        total_cost_usd=summary.total_cost_usd,
        num_turns=summary.num_turns,
        duration_ms=summary.duration_ms,
        input_tokens=summary.input_tokens,
        output_tokens=summary.output_tokens,
        cache_creation_tokens=summary.cache_creation_tokens,
        cache_read_tokens=summary.cache_read_tokens,
    )


def _assignment_to_usage(
    a: Assignment,
    *,
    logs_dir: Path | None = None,
    remote_data: dict | None = None,
) -> AssignmentUsage:
    """Build an :class:`AssignmentUsage` for *a*.

    Priority: local log file > *remote_data* dict (from agent HTTP) >
    Assignment.model field as a final model fallback.

    *remote_data* is a dict from the agent's ``list_assignments()`` response
    (e.g. ``{"cost_so_far": 0.12, "model_used": "claude-sonnet-4-6", ...}``).
    """
    _logs_dir = logs_dir if logs_dir is not None else LOGS_DIR
    usage = AssignmentUsage(
        assignment_id=a.assignment_id or "",
        repo_name=a.repo_name,
        issue_number=a.issue_number,
        issue_title=a.issue_title,
        status=a.status,
        model=a.model,
    )

    # Try local log first.
    if a.assignment_id:
        log_path = _logs_dir / f"{a.assignment_id}.log"
        parsed = parse_usage_from_log(log_path)
        if parsed is not None:
            usage.model = parsed.model or a.model
            usage.total_cost_usd = parsed.total_cost_usd
            usage.num_turns = parsed.num_turns
            usage.duration_ms = parsed.duration_ms
            usage.input_tokens = parsed.input_tokens
            usage.output_tokens = parsed.output_tokens
            usage.cache_creation_tokens = parsed.cache_creation_tokens
            usage.cache_read_tokens = parsed.cache_read_tokens
            return usage

    # Fall back to remote agent data if available.
    if remote_data:
        cost = remote_data.get("total_cost_usd") or remote_data.get("cost_so_far") or 0.0
        usage.total_cost_usd = float(cost)
        model_r = remote_data.get("model_used") or remote_data.get("model")
        if model_r:
            usage.model = str(model_r)
        turns = remote_data.get("num_turns") or remote_data.get("turns")
        if isinstance(turns, int):
            usage.num_turns = turns

    return usage


# ── Session collection ────────────────────────────────────────────────────────


def collect_usage(
    board_assignments: list[Assignment],
    *,
    logs_dir: Path | None = None,
    remote_by_id: dict[str, dict] | None = None,
) -> list[AssignmentUsage]:
    """Collect :class:`AssignmentUsage` for every assignment on the board.

    *remote_by_id* maps ``assignment_id → agent_status_dict`` for assignments
    whose logs live on a remote machine.  Pass ``None`` (the default) to skip
    remote lookups entirely — the result will still be correct for any
    assignment whose log is available locally.
    """
    result: list[AssignmentUsage] = []
    for a in board_assignments:
        if not a.assignment_id:
            continue
        remote = (remote_by_id or {}).get(a.assignment_id)
        result.append(_assignment_to_usage(a, logs_dir=logs_dir, remote_data=remote))
    return result


def build_session_usage(
    board_assignments: list[Assignment],
    *,
    logs_dir: Path | None = None,
    remote_by_id: dict[str, dict] | None = None,
    started_at: float | None = None,
) -> SessionUsage:
    """Build a :class:`SessionUsage` from the current board.

    *started_at* should come from ``session.json["started_at"]`` (parsed to
    a Unix timestamp).  If not provided we fall back to the oldest
    ``dispatched_at`` among the assignments.
    """
    if started_at is None:
        # Derive from oldest dispatch time on the board.
        times = [
            a.dispatched_at
            for a in board_assignments
            if a.dispatched_at is not None
        ]
        started_at = min(times) if times else None

    assignments = collect_usage(
        board_assignments,
        logs_dir=logs_dir,
        remote_by_id=remote_by_id,
    )
    return SessionUsage(started_at=started_at, assignments=assignments)


# ── Rollup fetch (#1118) ──────────────────────────────────────────────────────


def fetch_usage_rows(
    *,
    flag_url: str | None = None,
    flag_token: str | None = None,
    timeout: float = 5.0,
) -> list[dict]:
    """Fetch board assignment rows as plain dicts for :mod:`coord.usage_rollup`.

    This is the thin *fetch* caller the pure aggregator's docstring promises:
    it mirrors :func:`coord.board_service.read_board`'s local-vs-remote
    branch, but returns raw wire-shape ``dict`` rows rather than
    :class:`~coord.models.Assignment` instances — the aggregator consumes the
    daemon ``/board`` ``assignments`` shape directly, including
    ``is_interactive``, which is a real DB/wire column but (deliberately,
    #748/#632 — see :mod:`coord.board_bool_guard`) not an ``Assignment``
    dataclass field, so converting through ``Assignment`` would silently
    drop it.

    Remote reads the same ``/board`` endpoint ``coord status`` polls. Local
    reads the sqlite DB directly via ``SqliteStore.list_assignments()``
    rather than ``board_projection()["assignments"]`` — a usage rollup wants
    full history, not the retention-capped set ``/board`` serves the TUI.
    """
    from coord.client import fetch_board_payload, resolve_board_service  # noqa: PLC0415

    svc = resolve_board_service(flag_url, flag_token)
    if svc is not None:
        payload = fetch_board_payload(svc, timeout=timeout)
        return list(payload.get("assignments") or [])

    from coord.dao import SqliteStore  # noqa: PLC0415

    return SqliteStore().list_assignments()


# ── Formatting ────────────────────────────────────────────────────────────────


def _fmt_cost(usd: float) -> str:
    if usd < 0.001:
        return "$0.00"
    if usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:.2f}"


def _fmt_burn_rate(usd_per_hr: float) -> str:
    if usd_per_hr < 0.01:
        return f"${usd_per_hr:.4f}/hr"
    return f"${usd_per_hr:.2f}/hr"


def format_usage_report(session: SessionUsage) -> str:
    """Return the full multi-section usage report for ``coord usage``."""
    lines: list[str] = []

    # ── Session header ────────────────────────────────────────────────────
    burn = session.burn_rate_usd_per_hour()
    burn_str = _fmt_burn_rate(burn) if burn is not None else "(no session time)"
    high_flag = " ⚠" if burn is not None and burn >= HIGH_BURN_RATE_USD_PER_HOUR else ""

    n_done = sum(1 for a in session.assignments if a.status == "done")
    n_running = sum(1 for a in session.assignments if a.status == "running")
    n_failed = sum(1 for a in session.assignments if a.status == "failed")
    counts: list[str] = []
    if n_done:
        counts.append(f"{n_done} done")
    if n_running:
        counts.append(f"{n_running} running")
    if n_failed:
        counts.append(f"{n_failed} failed")
    counts_str = ", ".join(counts) if counts else "0 assignments"

    total_str = _fmt_cost(session.total_cost_usd)
    lines.append(f"Session usage:  {total_str}  •  {counts_str}  •  burn rate: {burn_str}{high_flag}")

    # Budget remaining estimate: max(0, 5hr - elapsed) * burn_rate
    elapsed_hours = session.elapsed_hours
    if elapsed_hours is not None and burn is not None and burn > 0:
        budget_remaining_usd = max(0.0, 5.0 - elapsed_hours) * burn
        lines.append(
            f"Est. 5hr budget remaining: ~{_fmt_cost(budget_remaining_usd)} (based on current rate)"
        )

    lines.append("")

    # ── Per-assignment table ──────────────────────────────────────────────
    if not session.assignments:
        lines.append("No assignments found.")
        return "\n".join(lines)

    lines.append("Per-assignment:")
    col_id_w = max(8, max(len(a.assignment_id[:8]) for a in session.assignments))
    col_repo_w = max(8, max(len(a.repo_name) for a in session.assignments))
    col_model_w = max(5, max(len(a.model or "(unknown)") for a in session.assignments))

    header = (
        f"  {'ID':<{col_id_w}}  {'STATUS':<7}  {'REPO':<{col_repo_w}}  "
        f"{'#':>5}  {'MODEL':<{col_model_w}}  {'TURNS':>5}  {'DUR':>7}  COST"
    )
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))

    for a in session.assignments:
        aid = (a.assignment_id or "")[:8]
        model = a.model or "(unknown)"
        dur = a.duration_str()
        cost = _fmt_cost(a.total_cost_usd)
        line = (
            f"  {aid:<{col_id_w}}  {a.status:<7}  {a.repo_name:<{col_repo_w}}  "
            f"#{a.issue_number:>4}  {model:<{col_model_w}}  {a.num_turns:>5}  "
            f"{dur:>7}  {cost}"
        )
        lines.append(line)

    # ── Token summary (only shown when any tokens were recorded) ──────────
    total_in = session.total_input_tokens
    total_out = session.total_output_tokens
    if total_in or total_out:
        lines.append("")
        lines.append(f"Token totals:  {total_in:,} input  •  {total_out:,} output")

    # ── Per-model breakdown ───────────────────────────────────────────────
    lines.append("")
    lines.append("Per-model:")
    cost_by = session.cost_by_model()
    count_by = session.count_by_model()
    total = session.total_cost_usd

    for model_name in sorted(cost_by, key=lambda m: cost_by[m], reverse=True):
        n = count_by[model_name]
        c = cost_by[model_name]
        pct = f"  ({100 * c / total:.0f}%)" if total > 0 else ""
        noun = "assignment" if n == 1 else "assignments"
        lines.append(f"  {model_name:<{col_model_w}}  {n} {noun:<12}  {_fmt_cost(c)}{pct}")

    return "\n".join(lines)


# ── Per-issue rollup rendering (#1115 CLI-1) ─────────────────────────────────
#
# Consumes coord.usage_rollup.aggregate() (#1118 Core) — this module only
# renders the plain dict it returns. Distinct 4-decimal cost formatting and
# compact token/duration formatting are used here (vs. the 2-decimal
# _fmt_cost/duration_str above) to match the sealed contract mocks exactly
# (tests/acceptance/ms-37/contract.md, Mocks 1 & 2).


def pricing_dict_from_config(pricing) -> dict:
    """Convert a :class:`~coord.config.PricingConfig` to the plain
    ``{model: {"input": ..., "output": ..., "cache_read": ..., "cache_creation": ...}}``
    dict :func:`coord.usage_rollup.aggregate` expects."""
    return {
        model: {
            "input": rates.input,
            "output": rates.output,
            "cache_read": rates.cache_read,
            "cache_creation": rates.cache_creation,
        }
        for model, rates in pricing.models.items()
    }


def _fmt_cost4(usd: float) -> str:
    """Captured-cost formatting for the rollup views: always 4 decimals."""
    return f"${usd:.4f}"


def _fmt_est4(usd: float) -> str:
    """Estimated-cost formatting: ``~$`` prefix, always visually distinct
    from a captured figure (see :func:`_fmt_cost4`)."""
    return f"~${usd:.4f}"


def _fmt_tokens_compact(n: int) -> str:
    """Compact token count: ``k`` below 1M, one-decimal ``M`` at/above."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{round(n / 1_000)}k"
    return str(n)


def _fmt_duration_hms(secs: float | None, *, is_open: bool) -> str:
    """``NmSSs`` duration (e.g. ``20m00s``); ``—`` for an open/unknown leg."""
    if is_open or secs is None:
        return "—"
    total = int(round(secs))
    m, s = divmod(total, 60)
    return f"{m}m{s:02d}s"


def format_usage_by_issue(result: dict, window_label: str) -> str:
    """Render the ``coord usage --by-issue`` view (contract Mock 1) from an
    :func:`coord.usage_rollup.aggregate` result (``by="issue"``).

    *window_label* is the resolved :class:`~coord.usage_rollup.Window`'s
    ``label`` (e.g. ``"today"``) — printed in the header, not consumed by
    the aggregator itself.
    """
    lines = [f"USAGE — by issue — window: {window_label}"]
    lines.append(
        f"{'issue':<8}{'repo':<10}{'legs':>4}   {'cost':<10} {'est(~)':<11} "
        f"{'out / cache':<20}{'time':<10}note"
    )
    for g in result["groups"]:
        issue_no = g["key"]
        repo = g["rows"][0].get("repo_name", "") if g["rows"] else ""
        cost_str = _fmt_cost4(g["cost_captured"]) if g["cost_captured"] > 0 else "—"
        est_str = _fmt_est4(g["cost_est"]) if g["cost_est"] > 0 else "—"
        out_str = _fmt_tokens_compact(g["tokens"]["output"])
        cache_str = _fmt_tokens_compact(g["tokens"]["cache_read"])
        tok_str = f"{out_str} / {cache_str}"
        dur_str = _fmt_duration_hms(g["duration_secs"], is_open=False)
        note = f"⚠ unknown-model:{g['unknown_models']}" if g["unknown_models"] else ""
        lines.append(
            f"#{issue_no:<7}{repo:<10}{g['legs']:>4}   {cost_str:<10} {est_str:<11} "
            f"{tok_str:<20}{dur_str:<10}{note}"
        )

    lines.append("─" * 80)
    t = result["totals"]
    total_out = _fmt_tokens_compact(t["tokens"]["output"])
    total_cache = _fmt_tokens_compact(t["tokens"]["cache_read"])
    total_dur = _fmt_duration_hms(t["duration_secs"], is_open=False)
    progress = f" · {t['open_legs']} in progress" if t["open_legs"] else ""
    lines.append(
        f"Σ  captured {_fmt_cost4(t['cost_captured'])} · est {_fmt_est4(t['cost_est'])} · "
        f"total {_fmt_cost4(t['cost_total'])} · {total_out} out / {total_cache} cache · "
        f"{total_dur}{progress}"
    )
    return "\n".join(lines)


def format_usage_issue_drill(rows: list[dict], issue_number: int, pricing) -> str:
    """Render the ``coord usage --issue N`` per-stage drill (contract Mock 2).

    *rows* are the raw board-row dicts for this one issue (any window
    filtering already applied by the caller); *pricing* is a
    :class:`~coord.config.PricingConfig`. Rows are rendered oldest-first by
    ``dispatched_at`` (falling back to ``finished_at``).
    """
    from coord.usage_rollup import leg_cost, leg_duration, parse_timestamp

    if not rows:
        return f"No usage data for issue #{issue_number}."

    def _sort_ts(row: dict) -> float:
        ts = parse_timestamp(row.get("dispatched_at"))
        if ts is None:
            ts = parse_timestamp(row.get("finished_at"))
        return ts if ts is not None else float("inf")

    ordered = sorted(rows, key=_sort_ts)
    repo = rows[0].get("repo_name", "")

    total_captured = 0.0
    total_est = 0.0
    for row in rows:
        captured, est, _unknown = leg_cost(row, pricing)
        total_captured += captured
        total_est += est

    lines = [
        f"#{issue_number}  {repo}   {_fmt_cost4(total_captured)} captured  +  "
        f"{_fmt_est4(total_est)} est"
    ]
    lines.append(
        f"{'stage':<9}{'model':<11}{'int':<5}{'cost':<11}{'est(~)':<11}"
        f"{'out':<7}{'cache':<8}{'time':<10}status"
    )
    for row in ordered:
        captured, est, unknown_model = leg_cost(row, pricing)
        duration, is_open = leg_duration(row)
        stage = str(row.get("type") or "")
        model = str(row.get("model") or "(unknown)")
        interactive = "I" if row.get("is_interactive") else "-"
        cost_col = _fmt_cost4(captured) if captured > 0 else "—"
        if est > 0:
            est_col = _fmt_est4(est)
        elif unknown_model:
            est_col = "n/a*"
        else:
            est_col = "—"
        out_col = _fmt_tokens_compact(int(row.get("output_tokens") or 0))
        cache_col = _fmt_tokens_compact(int(row.get("cache_read_tokens") or 0))
        time_col = _fmt_duration_hms(duration, is_open=is_open)
        status = str(row.get("status") or "")
        note = "  *unknown model" if unknown_model else ""
        lines.append(
            f"{stage:<9}{model:<11}{interactive:<5}{cost_col:<11}{est_col:<11}"
            f"{out_col:<7}{cache_col:<8}{time_col:<10}{status}{note}"
        )
    return "\n".join(lines)


# ── Cross-repo + time-bucketed rollup rendering (#1119 CLI-2) ────────────────
#
# Consumes coord.usage_rollup.aggregate() exactly like the #1115 renderers
# above — same ``~$``/4-decimal conventions, same plain-dict input. Two new
# views: a cross-cut rollup by repo/week/month (contract Mock 3) and a
# time-spent ranking by stage-type or issue (contract Mock 4).


def format_usage_by_group(result: dict, window_label: str, dim: str) -> str:
    """Render ``coord usage --by repo|week|month`` (contract Mock 3 for
    ``dim="repo"``) from an :func:`coord.usage_rollup.aggregate` result.

    Each row is one group (a repo name, or a week/month bucket string) with
    its issue count (distinct ``issue_number`` across the group's rows),
    legs, captured/estimated/total cost, tokens, and duration. Groups are
    rendered in *result*'s given order — callers sort beforehand (see
    ``coord/commands/status.py``'s ``_usage_sort_key``).
    """
    lines = [f"USAGE — by {dim} — window: {window_label}"]
    lines.append(
        f"{dim:<8}{'issues':>7}{'legs':>6}   {'cost':<10} {'est(~)':<11} "
        f"{'total':<11} {'out / cache':<20}time"
    )
    for g in result["groups"]:
        key_str = str(g["key"])
        issues = len({row.get("issue_number") for row in g["rows"]})
        cost_str = _fmt_cost4(g["cost_captured"]) if g["cost_captured"] > 0 else "—"
        est_str = _fmt_est4(g["cost_est"]) if g["cost_est"] > 0 else "—"
        total_str = _fmt_cost4(g["cost_total"])
        out_str = _fmt_tokens_compact(g["tokens"]["output"])
        cache_str = _fmt_tokens_compact(g["tokens"]["cache_read"])
        tok_str = f"{out_str} / {cache_str}"
        dur_str = _fmt_duration_hms(g["duration_secs"], is_open=False)
        lines.append(
            f"{key_str:<8}{issues:>7}{g['legs']:>6}   {cost_str:<10} {est_str:<11} "
            f"{total_str:<11} {tok_str:<20}{dur_str}"
        )

    lines.append("─" * 80)
    t = result["totals"]
    total_out = _fmt_tokens_compact(t["tokens"]["output"])
    total_cache = _fmt_tokens_compact(t["tokens"]["cache_read"])
    total_dur = _fmt_duration_hms(t["duration_secs"], is_open=False)
    progress = f" · {t['open_legs']} in progress" if t["open_legs"] else ""
    lines.append(
        f"Σ  total {_fmt_cost4(t['cost_total'])} · {total_out} out / {total_cache} cache · "
        f"{total_dur}{progress}"
    )
    return "\n".join(lines)


def format_usage_by_time(result: dict, window_label: str, dim: str) -> str:
    """Render ``coord usage --by-time`` (contract Mock 4) from an
    :func:`coord.usage_rollup.aggregate` result with ``by="stage"`` (default,
    ``dim="stage"``) or ``by="issue"`` (``dim="issue"``, via ``--by issue``).

    Ranks groups by share of total in-window active duration — "where is
    wall-clock going." An open leg (no ``finished_at``) contributes 0
    duration but is called out via the group's ``(N in progress)`` note.
    """
    lines = [f"USAGE — time by {dim} — window: {window_label}"]
    key_header = "issue" if dim == "issue" else "stage"
    lines.append(f"{key_header:<14}{'legs':>4}   {'time':<10}share")

    total_dur = result["totals"]["duration_secs"]
    for g in result["groups"]:
        key_str = f"#{g['key']}" if dim == "issue" else str(g["key"])
        dur_str = _fmt_duration_hms(g["duration_secs"], is_open=False)
        pct = f"{(g['duration_secs'] / total_dur * 100):.1f}%" if total_dur > 0 else "—"
        note = f"   ({g['open_legs']} in progress)" if g["open_legs"] else ""
        lines.append(f"{key_str:<14}{g['legs']:>4}   {dur_str:<10}{pct}{note}")

    lines.append(f"── total active {_fmt_duration_hms(total_dur, is_open=False)} ──")
    if dim == "stage":
        lines.append("(also available: --by-time --by issue → per-issue duration ranking)")
    else:
        lines.append("(also available: --by-time → time by stage)")
    return "\n".join(lines)


def format_burn_rate_line(session: SessionUsage) -> str | None:
    """One-line burn-rate summary for ``coord status``.

    Returns ``None`` when the burn rate is below the high threshold or
    can't be computed (no session time).
    """
    burn = session.burn_rate_usd_per_hour()
    if burn is None or burn < HIGH_BURN_RATE_USD_PER_HOUR:
        return None
    total_str = _fmt_cost(session.total_cost_usd)
    burn_str = _fmt_burn_rate(burn)
    n = len(session.assignments)
    noun = "assignment" if n == 1 else "assignments"
    return f"Usage: {total_str} this session  •  burn rate: {burn_str} ⚠  ({n} {noun})"
