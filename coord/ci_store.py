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
