"""CiStore abstraction over CI check status.

Phase 1 (#240) of the CiStore abstraction: a thin Protocol over ``gh pr checks``
so the merge gate can hard-block on failed checks and the TUI can surface what
broke. Rerun, polling, and non-GitHub backends are deferred to later phases.

The split between :class:`CiStore` (Protocol) and the concrete backends
(:class:`coord.ci_github.GitHubCi`, :class:`NoOpCi`) means tests can pass a
stub through ``ci_store=`` without touching subprocess at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class CiCheckSummary:
    """Structured rollup of a PR's CI checks — the board-wire analogue of the
    TUI's Rust ``CiCheckSummary`` (``tui/src/app/types.rs``).

    Populated server-side by :func:`summarize_counts` and attached to
    :class:`coord.merge_queue.PlannedMerge` so the TUI can render its "2✓ 1✗"
    badges straight from the ``/board`` payload instead of shelling out to
    ``gh pr checks`` itself (#1344).
    """

    passed: int
    failed: int
    running: int
    failed_names: list[str]
    first_failed_url: str | None


@dataclass
class CheckRun:
    """A single CI check run on a PR.

    ``status`` is the lifecycle phase: queued / in_progress / completed.
    ``conclusion`` is only meaningful when ``status == "completed"`` and is one
    of success / failure / cancelled / skipped / neutral / timed_out / action_required.
    Callers should treat ``conclusion in {"failure", "cancelled", "timed_out", "action_required"}``
    as a hard-fail and ``status != "completed"`` as in-flight.
    """

    name: str
    status: str
    conclusion: str | None
    url: str
    run_id: str
    started_at: float | None
    completed_at: float | None


@runtime_checkable
class CiStore(Protocol):
    """Read-only view of CI checks for a PR."""

    def list_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]: ...

    @property
    def is_available(self) -> bool: ...


class NoOpCi:
    """Always-available fallback that returns no checks.

    Used when the user opts out of CI gating with ``ci_store: { type: none }``
    or when no backend is configured.  ``is_available`` is ``False`` so callers
    can distinguish "no CI configured" from "CI says all clear".
    """

    def list_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]:
        return []

    @property
    def is_available(self) -> bool:
        return False


# ── Classification helpers ──────────────────────────────────────────────────

_FAILED_CONCLUSIONS = frozenset({"failure", "cancelled", "timed_out", "action_required"})


def failed_checks(checks: list[CheckRun]) -> list[CheckRun]:
    """Return checks whose conclusion indicates a hard failure."""
    return [c for c in checks if c.conclusion in _FAILED_CONCLUSIONS]


def in_flight_checks(checks: list[CheckRun]) -> list[CheckRun]:
    """Return checks that are queued or running (not yet completed)."""
    return [c for c in checks if c.status != "completed"]


def build_ci_store(ci_store_type: str) -> CiStore:
    """Construct the CiStore backend named by ``ci_store_type``.

    Centralised here so callers (merge gate, TUI fetcher, tests) don't need to
    branch on the config value themselves. Unknown types fall back to NoOpCi
    so a typo in coordinator.yml doesn't crash the merge command.
    """
    if ci_store_type == "github":
        from coord.ci_github import GitHubCi  # noqa: PLC0415
        return GitHubCi()
    return NoOpCi()


def summarize(checks: list[CheckRun]) -> str:
    """One-line summary: ``2✓ 1✗`` or ``no checks``.

    Used by the TUI under the Merge stage row and by the CLI when reporting
    why a merge was refused.
    """
    if not checks:
        return "no checks"
    passed = sum(1 for c in checks if c.conclusion == "success")
    failed = len(failed_checks(checks))
    running = len(in_flight_checks(checks))
    parts: list[str] = []
    if passed:
        parts.append(f"{passed}✓")
    if failed:
        parts.append(f"{failed}✗")
    if running:
        parts.append(f"{running}⋯")
    return " ".join(parts) if parts else "no checks"


def summarize_counts(checks: list[CheckRun]) -> CiCheckSummary:
    """Structured rollup of *checks*, mirroring the classification the TUI's
    (now-deleted) ``fetch_ci_check_summary`` used to compute client-side:

    - not yet ``completed`` → running
    - completed + conclusion in ``_FAILED_CONCLUSIONS`` → failed (name + first
      URL captured)
    - completed + any other conclusion (success / skipped / neutral) → passed

    Used to populate :class:`coord.merge_queue.PlannedMerge.ci_summary` so the
    `/board` payload carries everything the TUI renders as CI badges (#1344).
    """
    # `checks` items are `CheckRun` in production but tests commonly pass
    # lighter duck-typed fakes (see `failed_checks`/`in_flight_checks` above,
    # which only ever touch `.status`/`.conclusion`) — `getattr` with a
    # default keeps this function tolerant of fakes that omit `.url`.
    passed = failed = running = 0
    failed_names: list[str] = []
    first_failed_url: str | None = None
    for c in checks:
        if c.status != "completed":
            running += 1
            continue
        if c.conclusion in _FAILED_CONCLUSIONS:
            failed += 1
            failed_names.append(c.name)
            url = getattr(c, "url", "") or ""
            if first_failed_url is None and url:
                first_failed_url = url
        else:
            passed += 1
    return CiCheckSummary(
        passed=passed,
        failed=failed,
        running=running,
        failed_names=failed_names,
        first_failed_url=first_failed_url,
    )
