"""Text rendering for ``coord health`` (#1628).

**This module never decides anything.**  It reads ``result.severity`` and
``result.headroom`` and lays them out; it does not look at ``result.values``
except to pass it through, and it does not know what any particular check
measures.  That is the whole reason a check can be added without editing a
renderer — and the reason H-3's board projection and H-4's TUI/web renderers
can be written against the same :meth:`CheckResult.to_dict` contract without
re-deriving "is 86% used bad?" three more times.

If you find yourself wanting to special-case a check id in here, the thing
you actually want is another rendered field on ``CheckResult`` (like
``threshold`` or ``trend``), populated by the probe.
"""

from __future__ import annotations

from coord.health.models import CheckResult, Severity
from coord.health.registry import HealthReport

# Minimum column widths.  Columns grow to fit the widest row in the report
# (see :func:`render_report`) rather than truncating — a clipped repo name or
# path in a health report costs more than a ragged right edge.
_LABEL_WIDTH = 20
_SEVERITY_WIDTH = 5
_HEADROOM_WIDTH = 46
# Don't let one pathological label push every other column off the screen.
_MAX_LABEL_WIDTH = 40


def render_result(result: CheckResult, *, label_width: int = _LABEL_WIDTH) -> str:
    """One report line: ``<label>  <SEVERITY>  <headroom>  <threshold>``."""
    label = result.label.ljust(label_width)
    severity = result.severity.label.ljust(_SEVERITY_WIDTH)
    headroom = result.headroom
    trailer_parts = []
    if result.trend:
        trailer_parts.append(result.trend)
    # The threshold reminder is only useful next to a number that is near it.
    # On an OK line it is clutter, so it rides along only when something is up.
    if result.threshold and result.severity is not Severity.OK:
        trailer_parts.append(result.threshold)
    trailer = "  ".join(trailer_parts)
    line = f"{label}  {severity}  {headroom}"
    if trailer:
        line = f"{line.ljust(label_width + _SEVERITY_WIDTH + _HEADROOM_WIDTH + 4)}  {trailer}"
    return line.rstrip()


def render_report(report: HealthReport, *, verbose: bool = False) -> str:
    """The full ``coord health`` body.

    ``verbose`` adds each result's ``detail`` (the "fix: ..." line) as an
    indented continuation; without it only the headroom is shown, which is
    what makes the report scannable at a glance.
    """
    label_width = min(
        _MAX_LABEL_WIDTH,
        max([_LABEL_WIDTH, *(len(r.label) for r in report.results)]),
    )
    lines: list[str] = []
    for result in report.results:
        lines.append(render_result(result, label_width=label_width))
        if result.detail and (verbose or result.severity is not Severity.OK):
            lines.append(f"{' ' * (label_width + 2)}  {result.detail}")
    if not lines:
        lines.append("no checks ran")
    for skipped in report.skipped:
        lines.append(f"{'skipped'.ljust(label_width)}  -      {skipped}")
    lines.append(render_summary(report))
    return "\n".join(lines)


def render_summary(report: HealthReport) -> str:
    """The trailer line — machine-greppable, same shape as GRAPH_HEALTH's."""
    counts = report.counts()
    return (
        f"HEALTH: {report.severity.label} "
        f"crit={counts['crit']} warn={counts['warn']} "
        f"unknown={counts['unknown']} ok={counts['ok']} "
        f"in {report.duration_secs:.2f}s"
    )
