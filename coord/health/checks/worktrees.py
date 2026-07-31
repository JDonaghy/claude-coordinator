"""Stale coordinator worktrees under ``~/.coord/worktrees`` (#1628).

Every dispatched worker gets an ephemeral checkout here and
``AgentServer._cleanup_worktree`` is supposed to remove it.  When cleanup
doesn't run — a killed daemon, a worker that died mid-task, a dirty tree the
pruner correctly refuses to delete — the directory survives, and each one
carries a full checkout (and, on Rust repos, potentially a ``target/``).
They accumulate silently.

**"Stale" here is deliberately mtime-based, not liveness-based.**
``coord diagnose --orphan-worktrees`` already does the precise thing —
cross-reference each worktree's assignment id against live tmux sessions and
running DB rows — but that needs board state, and board state is H-3's job,
not this child's.  Reading a directory's mtime is one ``stat`` per entry, no
DB, no network, and a worktree nothing has touched in two days is a
perfectly good proxy for one nobody is using.  When the fleet-scope probes
land they can sharpen this; until then it is honest and cheap.
"""

from __future__ import annotations

from pathlib import Path

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check
from coord.health.units import human_hours


@check(
    id="worktrees",
    scope="machine",
    title="worktrees",
    order=30,
    description="Coordinator worktrees nothing has touched recently.",
)
def probe_worktrees(ctx: HealthContext) -> CheckResult | None:
    th = ctx.thresholds
    root: Path = ctx.coord_dir / "worktrees"
    try:
        entries = [e for e in root.iterdir() if e.is_dir() and not e.is_symlink()]
    except OSError:
        # No worktrees dir at all is the normal state on a machine that has
        # never been dispatched to — not a finding.
        return None

    stale_cutoff = ctx.now - th.worktree_stale_hours * 3600.0
    stale: list[tuple[str, float]] = []
    for entry in entries:
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime < stale_cutoff:
            stale.append((entry.name, ctx.now - mtime))

    count = len(stale)
    if count > th.worktree_crit_count:
        severity = Severity.CRIT
    elif count > th.worktree_warn_count:
        severity = Severity.WARN
    else:
        severity = Severity.OK

    stale.sort(key=lambda pair: pair[1], reverse=True)
    if count == 0:
        headroom = f"0 stale of {len(entries)}"
    else:
        oldest_name, oldest_age = stale[0]
        headroom = (
            f"{count} stale of {len(entries)} "
            f"(oldest {oldest_name} {human_hours(oldest_age)})"
        )

    return CheckResult(
        check_id="worktrees",
        scope="machine",
        severity=severity,
        headroom=headroom,
        threshold=f"crit above {th.worktree_crit_count}",
        detail=(
            "prune with `coord diagnose --orphan-worktrees`"
            if severity is not Severity.OK
            else ""
        ),
        values={
            "root": str(root),
            "total": len(entries),
            "stale_count": count,
            "stale_hours_threshold": th.worktree_stale_hours,
            "stale": [
                {"name": name, "age_hours": round(age / 3600.0, 2)} for name, age in stale
            ],
            "warn_count": th.worktree_warn_count,
            "crit_count": th.worktree_crit_count,
        },
    )
