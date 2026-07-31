"""Total size of every cargo ``target/`` directory this machine keeps (#1628).

The 2026-07-30 incident: **78G** of cargo build artifacts across a handful of
checkouts plus ``~/.coord/cargo-target`` — which is what actually consumed
``/home``.  ``coord.cargo_cache`` already GCs the *shared* cache (default 20
GiB cap), but nothing totals it together with the per-checkout ``target/``
dirs a human created by building in a live checkout, and that sum is the
number that fills a disk.

Cost note: this is the one seed probe that can be genuinely slow, because
"how big is 78G of small files" is a full tree walk.  It is therefore
budgeted (``health.cargo_scan_budget_secs``, default 1.5s) and reports a
partial scan when it runs out.  A partial total is a **lower bound**, so a
CRIT derived from one is still correct; only an OK is downgraded (to
``unknown``) when the scan didn't finish, because "we didn't finish looking"
must never render as "nothing there".
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check
from coord.health.units import expand, gib, human_bytes, shorten_path


def _dir_size_budgeted(path: Path, deadline: float) -> tuple[int, bool]:
    """``(bytes, complete)`` for the regular files under *path*.

    Symlinked subdirectories are never followed — a worktree's ``target``
    symlinked at the shared cache must not be counted twice, and a symlink
    out of the tree must not be counted at all.
    """
    total = 0
    complete = True
    checked = 0
    for root, dirnames, filenames in os.walk(path, onerror=lambda _e: None):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(root, d))]
        for name in filenames:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
        # time.monotonic() per file would dominate the walk on a tree with
        # a million small files; per 200 entries is accurate enough for a
        # 1.5s budget and costs nothing.
        checked += len(filenames) + 1
        if checked >= 200:
            checked = 0
            if time.monotonic() >= deadline:
                complete = False
                break
    return total, complete


def _candidate_dirs(ctx: HealthContext) -> list[Path]:
    """Every cargo target dir we know about, deduped, existing only.

    Three sources, in report order:

    1. ``~/.coord/cargo-target/<repo>`` — the shared per-machine cache
       (``coord.cargo_cache``), one subdirectory per repo.
    2. ``<checkout>/target`` for each checkout in ``coordinator.yml`` — what
       a human building in the live checkout creates, invisible to the cache
       GC.
    3. ``health.cargo_target_extra_dirs`` — anything else on this box.
    """
    from coord.cargo_cache import CACHE_DIRNAME  # noqa: PLC0415

    out: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        try:
            resolved = p.resolve()
        except OSError:
            resolved = p
        if resolved in seen or not p.is_dir():
            return
        seen.add(resolved)
        out.append(p)

    cache_root = ctx.coord_dir / CACHE_DIRNAME
    try:
        for entry in sorted(cache_root.iterdir()):
            if entry.is_dir() and not entry.is_symlink():
                _add(entry)
    except OSError:
        pass

    for checkout in ctx.checkouts:
        _add(checkout.path / "target")

    for raw in getattr(ctx.thresholds, "cargo_target_extra_dirs", ()) or ():
        _add(expand(raw, ctx.home))

    return out


@check(
    id="cargo_targets",
    scope="machine",
    title="cargo targets",
    order=20,
    description="Total size of cargo build artifacts across known target dirs.",
)
def probe_cargo_targets(ctx: HealthContext) -> CheckResult | None:
    """One machine-scope result: the total, with the biggest offenders named."""
    th = ctx.thresholds
    dirs = _candidate_dirs(ctx)
    if not dirs:
        return None  # no Rust on this box — silence beats a green line

    deadline = time.monotonic() + max(0.05, float(th.cargo_scan_budget_secs))
    sizes: list[tuple[Path, int]] = []
    complete = True
    for d in dirs:
        size, done = _dir_size_budgeted(d, deadline)
        sizes.append((d, size))
        if not done:
            complete = False
            break
    # Directories we never reached at all still count as "didn't finish".
    if len(sizes) < len(dirs):
        complete = False

    total = sum(s for _, s in sizes)
    total_gb = gib(total)

    if total_gb > th.cargo_target_crit_gb:
        severity = Severity.CRIT
    elif total_gb > th.cargo_target_warn_gb:
        severity = Severity.WARN
    elif complete:
        severity = Severity.OK
    else:
        # Below WARN but we stopped early: the real total could be anything.
        severity = Severity.UNKNOWN

    biggest = sorted(sizes, key=lambda pair: pair[1], reverse=True)[:3]
    breakdown = ", ".join(
        f"{shorten_path(str(p), str(ctx.home))} {human_bytes(s)}" for p, s in biggest if s > 0
    )
    headroom = human_bytes(total)
    if breakdown:
        headroom = f"{headroom}  ({breakdown})"
    if not complete:
        headroom = f"{headroom} [partial scan — {th.cargo_scan_budget_secs}s budget hit]"

    return CheckResult(
        check_id="cargo_targets",
        scope="machine",
        severity=severity,
        headroom=headroom,
        threshold=f"crit at {th.cargo_target_crit_gb:.0f}G",
        error=None if complete else "scan budget exhausted; total is a lower bound",
        values={
            "total_bytes": total,
            "total_gb": round(total_gb, 2),
            "complete": complete,
            "dirs": [
                {"path": str(p), "bytes": s, "gb": round(gib(s), 2)}
                for p, s in sorted(sizes, key=lambda pair: pair[1], reverse=True)
            ],
            "warn_gb": th.cargo_target_warn_gb,
            "crit_gb": th.cargo_target_crit_gb,
        },
    )
