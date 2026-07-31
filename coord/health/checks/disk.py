"""Disk headroom per configured mount point (#1628).

The 2026-07-30 incident that motivated this milestone: elitebook's ``/home``
reached **0 bytes free** and every worker on that machine failed in a way
that looked like a coordinator bug.  Nothing was watching the one number
that would have said so a day earlier.

Thresholds are expressed as percent *free* — headroom — rather than percent
used, because "how much is left" is the question, and because the rendered
line still shows used% for the operator who thinks in those terms.
"""

from __future__ import annotations

import os
import shutil

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check
from coord.health.units import expand, human_bytes


@check(
    id="disk",
    scope="machine",
    title="disk",
    order=10,
    description="Free space on the filesystems the fleet writes to.",
)
def probe_disk(ctx: HealthContext) -> list[CheckResult]:
    """One result per distinct filesystem behind ``thresholds.disk_paths``.

    Paths that don't exist are skipped silently (``/home`` is not a separate
    mount everywhere).  Paths that resolve to a filesystem already reported
    are skipped too — on a single-root machine ``/``, ``/home`` and
    ``~/.coord`` are the same device, and three identical CRIT lines is noise
    that trains an operator to skim.
    """
    th = ctx.thresholds
    results: list[CheckResult] = []
    seen_devices: set[int] = set()

    for raw in th.disk_paths:
        path = expand(raw, ctx.home)
        try:
            device = os.stat(path).st_dev
        except OSError:
            continue  # not present on this machine — not a finding
        if device in seen_devices:
            continue
        seen_devices.add(device)

        try:
            usage = shutil.disk_usage(str(path))
        except OSError as exc:
            results.append(
                CheckResult(
                    check_id="disk",
                    scope="machine",
                    subject=str(raw),
                    severity=Severity.UNKNOWN,
                    headroom=f"could not stat filesystem: {exc}",
                    error=str(exc),
                )
            )
            continue

        total = float(usage.total)
        free_pct = (usage.free / total * 100.0) if total > 0 else 0.0
        used_pct = 100.0 - free_pct

        if free_pct < th.disk_crit_free_pct:
            severity = Severity.CRIT
        elif free_pct < th.disk_warn_free_pct:
            severity = Severity.WARN
        else:
            severity = Severity.OK

        results.append(
            CheckResult(
                check_id="disk",
                scope="machine",
                subject=str(raw),
                severity=severity,
                headroom=f"{used_pct:.0f}% used ({human_bytes(usage.free)} free)",
                threshold=f"crit at {100.0 - th.disk_crit_free_pct:.0f}%",
                values={
                    "path": str(path),
                    "total_bytes": usage.total,
                    "free_bytes": usage.free,
                    "used_bytes": usage.used,
                    "free_pct": round(free_pct, 2),
                    "used_pct": round(used_pct, 2),
                    "warn_free_pct": th.disk_warn_free_pct,
                    "crit_free_pct": th.disk_crit_free_pct,
                },
            )
        )
    return results
