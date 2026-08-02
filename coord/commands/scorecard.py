"""`coord scorecard` — the dogfood scorecard (#1559): per-issue and
per-milestone evidence for "is this good enough to sell?", not just code.

**Why a new command, not an extension of ``coord usage``** (#1559 asks for
this to be justified): ``coord.usage_rollup``'s public contract (#1118) is a
*pure* cost/token/duration aggregator with no I/O and no notion of GitHub
labels or human-attended dispatch shape — that boundary is what lets
``coord usage``, the TUI Time view, and this command all share one rollup
without three reimplementations. Grafting first-pass-acceptance,
intervention classification, and label-sourced escaped-defect/process-bug
reads onto it would blow that boundary open for a use case ``coord usage``
callers don't need. Scorecard instead *composes* three existing read seams
exactly once each, at this CLI layer, and hands the results to the pure
:mod:`coord.scorecard` aggregator:

- :func:`coord.usage.fetch_usage_rows` — the same board-row fetch
  ``coord usage`` uses (daemon ``/board`` when configured, local DB else).
- :func:`coord.state.list_audit_log` — a best-effort durability cross-check
  for "merged" (see :mod:`coord.scorecard`'s docstring). Paginated back to
  the milestone's own creation date (see ``_AUDIT_LOG_MAX_PAGES`` below) —
  a single newest-first page can't be trusted to reach an old milestone in
  a repo with a long merge history.
- :func:`coord.github_ops.get_milestone_issues` — milestone membership
  (open+closed) with labels, the source for the two label conventions.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import click

from coord.commands._common import _CONFIG_OPTION, _load_config


# The audit log is paginated at audit.MAX_LIMIT (500) per page, newest-first.
# For a repo with a long merge history, "the 500 most recent merged events"
# may not reach back to an older milestone at all — silently emptying the
# merge-durability cross-check for exactly the historical-validation case
# #1559 cares about. We bound the query with the milestone's own creation
# date (nothing can have merged for it before it existed) and paginate the
# rest of the way back to that bound, capped so one CLI invocation can't
# turn into an unbounded DB scan.
_AUDIT_LOG_PAGE_SIZE = 500
_AUDIT_LOG_MAX_PAGES = 20  # 20 * 500 = 10,000 entries


def _parse_github_timestamp(value: object) -> float | None:
    """Parse a GitHub REST API ISO-8601 timestamp (e.g.
    ``"2024-01-01T00:00:00Z"``) into a Unix epoch float. Returns ``None`` for
    missing/malformed input so callers degrade to "no time bound" instead of
    raising — the audit cross-check is best-effort end to end."""
    if not value:
        return None
    from datetime import datetime  # noqa: PLC0415

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _fmt_usd(usd: float) -> str:
    return f"${usd:.2f}"


def _fmt_hms(secs: float) -> str:
    total = int(round(secs))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _fmt_rate(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.0f}%"


def _fmt_first_pass(value: str) -> str:
    return {"yes": "yes", "no": "no", "unknown": "?"}.get(value, value)


def format_scorecard(card, *, verbose: bool = False) -> str:
    """Human-readable rendering of a :class:`coord.scorecard.MilestoneScorecard`."""
    t = card.totals
    lines = [
        f"DOGFOOD SCORECARD — {card.repo_name} milestone #{card.milestone} "
        f"\"{card.milestone_title}\" ({t['issue_count']} issues)",
        "",
        f"First-pass acceptance:  {t['first_pass']['yes']}/"
        f"{t['first_pass']['yes'] + t['first_pass']['no']} = "
        f"{_fmt_rate(t['first_pass']['rate'])}"
        f"   (unknown: {t['first_pass']['unknown']})",
        f"Human interventions:    {t['interventions']['total']}"
        f"  (fix={t['interventions']['by_kind']['fix']}"
        f" rescue={t['interventions']['by_kind']['rescue']}"
        f" nudge={t['interventions']['by_kind']['nudge']}"
        f" abandon={t['interventions']['by_kind']['abandon']})"
        f"   [{t['interventions']['issues_with_unknown_data']} issues have no"
        f" board data — excluded from the count above;"
        f" {t['interventions']['issues_with_multiple_roots']} issues have a"
        f" second unrelated root dispatch — first_pass=no but not counted"
        " in any kind above]",
        f"Cost + wall-clock:      {_fmt_usd(t['cost']['total_usd'])} /"
        f" {_fmt_hms(t['cost']['duration_secs'])}"
        f"   ({t['cost']['issues_with_data']}/{t['issue_count']} issues have"
        f" cost data; {t['cost']['issues_without_data']} unknown)",
        "Escaped defects:        "
        + " ".join(
            f"{stage}={t['escaped_defects']['by_stage'][stage]}"
            for stage in t["escaped_defects"]["by_stage"]
        )
        + f"   ({t['escaped_defects']['issues_unlabeled']} issues unlabeled —"
        " NOT the same as zero)",
        f"Process bugs surfaced:  {t['process_bugs']['count']}"
        f"   regression test: landed={t['process_bugs']['regression_test']['landed']}"
        f" missing={t['process_bugs']['regression_test']['missing']}"
        f" unknown={t['process_bugs']['regression_test']['unknown']}",
    ]

    if verbose:
        lines.append("")
        lines.append(
            f"{'#':<8}{'FIRST-PASS':<11}{'INTERVENTIONS':<15}{'COST':<10}"
            f"{'TIME':<9}{'ESCAPED':<12}{'PROCESS-BUG':<14}TITLE"
        )
        for c in card.issues:
            interv = sum(c.interventions.values())
            interv_str = f"{interv}" if c.has_assignment_data else "?"
            if c.multiple_roots:
                interv_str += "+multiroot"
            cost_str = _fmt_usd(c.cost_total) if c.has_cost_data else "?"
            time_str = _fmt_hms(c.duration_secs) if c.has_cost_data else "?"
            escaped_str = c.escaped_defect_stage or "-"
            pb_str = (
                f"{c.regression_test}" if c.process_bug else "-"
            )
            title = c.title if len(c.title) <= 40 else c.title[:39] + "…"
            lines.append(
                f"#{c.number:<7}{_fmt_first_pass(c.first_pass):<11}{interv_str:<15}"
                f"{cost_str:<10}{time_str:<9}{escaped_str:<12}{pb_str:<14}{title}"
            )

    return "\n".join(lines)


@click.command(
    "scorecard",
    help=(
        "The dogfood scorecard (#1559): first-pass acceptance, human "
        "interventions, cost + wall-clock, escaped defects, and process bugs "
        "surfaced — per issue and aggregated, for one GitHub milestone.\n\n"
        "REPO is the local repo name from coordinator.yml; MILESTONE is the "
        "GitHub milestone NUMBER (not the tracking-issue number)."
    ),
)
@click.argument("repo")
@click.argument("milestone", type=int)
@click.option(
    "--verbose", "-v", is_flag=True, default=False,
    help="Also print the per-issue table (human output only; --json always includes it).",
)
@click.option("--json", "output_json", is_flag=True, default=False, help="Output machine-readable JSON.")
@_CONFIG_OPTION
def scorecard(
    repo: str,
    milestone: int,
    verbose: bool,
    output_json: bool,
    config_path: Path,
) -> None:
    from coord import github_ops  # noqa: PLC0415
    from coord.scorecard import build_milestone_scorecard, scorecard_to_dict  # noqa: PLC0415
    from coord.state import list_audit_log  # noqa: PLC0415
    from coord.usage import fetch_usage_rows, pricing_dict_from_config  # noqa: PLC0415

    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r} (not in coordinator.yml)", err=True)
        raise SystemExit(2)
    slug = repo_entry.github

    try:
        ms = github_ops.get_milestone(slug, milestone)
    except Exception as e:  # noqa: BLE001 — surface a clean CLI error
        click.echo(f"error: could not resolve milestone {milestone} on {slug}: {e}", err=True)
        raise SystemExit(1) from e
    ms_title = ms.get("title") or str(milestone)

    try:
        issues = github_ops.get_milestone_issues(slug, ms_title, state="all")
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: could not list issues for milestone {ms_title!r}: {e}", err=True)
        raise SystemExit(1) from e

    try:
        assignment_rows = fetch_usage_rows()
    except Exception as e:  # noqa: BLE001 — board data is best-effort here;
        # an unreachable daemon shouldn't block the label-only metrics.
        click.echo(f"warning: could not fetch board assignment rows: {e}", err=True)
        assignment_rows = []

    # Best-effort durability cross-check (see coord.scorecard's docstring) —
    # never fatal; a slow/unreachable audit read just means the cross-check
    # is skipped, board `status="merged"` remains the primary signal. Bound
    # by the milestone's own creation date and paginate back to that bound
    # (see _AUDIT_LOG_* constants above) rather than trusting a single
    # newest-first page to reach an old milestone.
    audit_entries: list[dict] = []
    since_ts = _parse_github_timestamp(ms.get("created_at"))
    try:
        cursor: str | None = None
        for _page in range(_AUDIT_LOG_MAX_PAGES):
            audit_result = list_audit_log(
                repo=repo, category="merge", event_type="merged",
                since=since_ts, limit=_AUDIT_LOG_PAGE_SIZE, cursor=cursor,
            )
            audit_entries.extend(audit_result.get("entries") or [])
            cursor = audit_result.get("next_cursor")
            if not audit_result.get("has_more") or not cursor:
                break
        else:
            click.echo(
                "warning: audit log merge cross-check stopped after "
                f"{_AUDIT_LOG_MAX_PAGES * _AUDIT_LOG_PAGE_SIZE} entries "
                "(more were available) — durability cross-check may be "
                "incomplete for the oldest issues in this milestone",
                err=True,
            )
    except Exception as e:  # noqa: BLE001
        click.echo(f"warning: could not fetch audit log: {e}", err=True)

    card = build_milestone_scorecard(
        milestone=milestone,
        milestone_title=ms_title,
        repo_name=repo,
        issues=issues,
        assignment_rows=assignment_rows,
        audit_entries=audit_entries,
        pricing=pricing_dict_from_config(cfg.pricing),
    )

    if output_json:
        click.echo(_json.dumps(scorecard_to_dict(card), indent=2, default=str))
        return

    click.echo(format_scorecard(card, verbose=verbose))
