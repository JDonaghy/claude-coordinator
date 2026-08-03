"""graphify graph freshness per checkout (#1628).

**This is a wrapper, not a reimplementation.**  ``coord.graph_health``
already knows how to answer both halves — :func:`~coord.graph_health.graph_status`
compares ``GRAPH_REPORT.md``'s "Built from commit" against HEAD (with the
``manifest.json``-mtime escape hatch that stops a genuinely-current graph
from reporting STALE forever), and
:func:`~coord.graph_health.hooks_path_status` checks the ``core.hooksPath``
setting that decides whether anything will ever rebuild it.  ``coord
diagnose --graph`` renders them today.  Forking either would guarantee the
two surfaces drift apart, which is the exact failure this milestone's
"renderers must never re-derive severity" rule exists to prevent — so this
module calls them and maps their output to a severity.

**Why hooks-disabled makes a stale graph CRIT rather than WARN.**  A stale
graph with working hooks is a nuisance: the next commit rebuilds it.  A
stale graph on a checkout with ``core.hooksPath`` unset (or with hooks
orphaned into ``.git/hooks``, which git ignores entirely once
``core.hooksPath`` is set) **will not self-heal at any point in the future**
— it stays wrong until a human runs ``graphify update`` by hand, and every
agent that queries it in the meantime gets answers about a commit that is no
longer HEAD.  That is the 2026-07-30 vimcode incident: 128.8h stale, hooks
disabled, and nothing in the fleet said so.  Age escalates a
hooks-working checkout from WARN to CRIT at ``graph_stale_crit_hours``;
hooks-disabled skips straight to CRIT because time cannot fix it.
"""

from __future__ import annotations

import subprocess

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check
from coord.health.units import human_hours


def _commits_behind(repo_path: str, built_sha: str, head_sha: str) -> int | None:
    """How many commits separate ``built_sha`` from ``head_sha``.

    Purely informational — an operator (and #1728's H-6 successor, deciding
    how urgently to rebuild) reads better from "13 commits behind" than from
    an age in hours, which conflates "how stale" with "how busy was the
    repo". This does NOT feed severity: :func:`~coord.graph_health.graph_status`
    already decided ``stale`` from the SHA comparison plus the
    manifest-mtime escape hatch, and duplicating that decision here from a
    commit count would be exactly the second copy this module's docstring
    warns against. Best-effort: returns ``None`` (never raises) when the
    shas are abbreviated past what git can resolve, the repo can't be read,
    or the call times out.
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), "rev-list", "--count", f"{built_sha}..{head_sha}"],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


@check(
    id="graph",
    scope="checkout",
    title="graph",
    order=70,
    description="graphify graph freshness vs HEAD, and whether hooks can heal it.",
)
def probe_graph(ctx: HealthContext) -> list[CheckResult]:
    from coord.graph_health import graph_status, hooks_path_status  # noqa: PLC0415

    th = ctx.thresholds
    results: list[CheckResult] = []

    for checkout in ctx.checkouts:
        status = graph_status(checkout.path)
        hooks_ok, hooks_detail = hooks_path_status(checkout.path)

        values = {
            "path": str(checkout.path),
            "present": status.present,
            "in_sync": status.in_sync,
            "stale": status.stale,
            "stamp_behind": status.stamp_behind,
            "verified_current": status.verified_current,
            "built_sha": status.built_sha,
            "head_sha": status.head_sha,
            "age_seconds": status.age_seconds,
            "age_hours": (
                round(status.age_seconds / 3600.0, 1) if status.age_seconds is not None else None
            ),
            "is_symlink": status.is_symlink,
            "hooks_ok": hooks_ok,
            "hooks_detail": hooks_detail,
            "unknown_reason": status.unknown_reason,
            "warn_hours": th.graph_stale_warn_hours,
            "crit_hours": th.graph_stale_crit_hours,
        }

        if not status.present:
            # No graph at all.  Agents are told to query it first (CLAUDE.md),
            # so its absence silently downgrades every one of them to grep.
            results.append(
                CheckResult(
                    check_id="graph",
                    scope="checkout",
                    subject=checkout.name,
                    severity=Severity.WARN,
                    headroom="no graph built here",
                    detail=status.unknown_reason or "",
                    threshold="warn when absent",
                    values=values,
                )
            )
            continue

        if status.unknown_reason and not status.stamp_behind:
            results.append(
                CheckResult(
                    check_id="graph",
                    scope="checkout",
                    subject=checkout.name,
                    severity=Severity.UNKNOWN,
                    headroom=f"freshness unknown — {status.unknown_reason}",
                    error=status.unknown_reason,
                    values=values,
                )
            )
            continue

        age_hours = (status.age_seconds or 0.0) / 3600.0
        age_text = human_hours(status.age_seconds) if status.age_seconds is not None else "?h"

        # #1728: how many commits separate the stamp from HEAD.  Purely
        # additive to the message — severity above is still `status.stale`
        # (SHA comparison + the manifest-mtime escape hatch), never this
        # count, so a repo that made 1 commit vs. 100 since the graph was
        # built is still judged identically on "is it stale", only described
        # differently.
        commits_behind: int | None = None
        if status.stale and status.built_sha and status.head_sha:
            commits_behind = _commits_behind(checkout.path, status.built_sha, status.head_sha)
        values["commits_behind"] = commits_behind
        commits_suffix = (
            ""
            if commits_behind is None
            else f", {commits_behind} commit{'' if commits_behind == 1 else 's'} behind"
        )

        if not status.stale:
            severity = Severity.OK
            headroom = f"in sync ({(status.built_sha or '')[:8]}), {age_text} old"
            if status.verified_current and status.stamp_behind:
                headroom = f"content current (stamp {(status.built_sha or '')[:8]}), {age_text} old"
        elif not hooks_ok:
            severity = Severity.CRIT
            headroom = f"{age_text} stale{commits_suffix}, hooks disabled -> will not self-heal"
        elif age_hours >= th.graph_stale_crit_hours:
            severity = Severity.CRIT
            headroom = f"{age_text} stale{commits_suffix} (HEAD {(status.head_sha or '')[:8]})"
        elif age_hours >= th.graph_stale_warn_hours:
            severity = Severity.WARN
            headroom = f"{age_text} stale{commits_suffix} (HEAD {(status.head_sha or '')[:8]})"
        else:
            severity = Severity.WARN
            headroom = f"stale, {age_text} old{commits_suffix} (HEAD {(status.head_sha or '')[:8]})"

        detail = ""
        if severity is not Severity.OK:
            detail = f"fix: graphify update {checkout.path}"
            if not hooks_ok:
                detail = f"{detail}  —  {hooks_detail}"

        results.append(
            CheckResult(
                check_id="graph",
                scope="checkout",
                subject=checkout.name,
                severity=severity,
                headroom=headroom,
                detail=detail,
                threshold=f"crit at {th.graph_stale_crit_hours:.0f}h (or any age with hooks off)",
                values=values,
            )
        )
    return results
