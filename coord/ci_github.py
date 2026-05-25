"""GitHub Actions backend for :mod:`coord.ci_store`.

Shells out to ``gh pr checks <number> --repo <slug> --json …`` and maps the
response to :class:`coord.ci_store.CheckRun`.  Results are cached per-(repo,
number) for ``cache_ttl`` seconds so the merge gate (which may iterate over
many PRs) doesn't hammer ``gh`` — the cost of a stale read in the gate path
is at most one wasted retry, and the user will re-run ``coord merge`` anyway.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime

from coord.ci_store import CheckRun


def _parse_ts(raw: str | None) -> float | None:
    """Parse an ISO-8601 timestamp from gh (e.g. ``2026-05-24T12:34:56Z``).

    Returns ``None`` for empty / unparseable input — gh emits an empty string
    when the field is unknown rather than omitting the JSON key.
    """
    if not raw:
        return None
    try:
        # gh emits Zulu; datetime.fromisoformat accepts the +00:00 form.
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _normalize_status(state: str) -> str:
    """Map gh's ``state`` field to the lifecycle enum used by CheckRun.

    gh's ``state`` is one of QUEUED / IN_PROGRESS / COMPLETED / PENDING and
    historically the casing varies between gh versions. We normalise to lower
    snake-case so the merge gate's predicates are stable.
    """
    s = (state or "").lower()
    if s in ("queued", "pending"):
        return "queued"
    if s in ("in_progress", "running"):
        return "in_progress"
    if s in ("completed", "complete"):
        return "completed"
    # Unknown — treat as in-flight so the gate refuses rather than allowing.
    return "in_progress"


def _normalize_conclusion(raw: str | None) -> str | None:
    """gh emits an empty string for ``conclusion`` until the check finishes."""
    if not raw:
        return None
    return raw.lower()


@dataclass
class GitHubCi:
    """Shell out to ``gh pr checks`` and cache results briefly."""

    cache_ttl: float = 10.0
    _cache: dict[tuple[str, int], tuple[float, list[CheckRun]]] = field(default_factory=dict)

    @property
    def is_available(self) -> bool:
        # gh is a hard dependency of the project (see CLAUDE.md). The
        # subprocess check is cheap but unnecessary; assume True when this
        # backend is constructed and let the actual ``gh pr checks`` call
        # surface the failure if gh is missing.
        return True

    def list_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]:
        key = (repo, number)
        now = time.time()
        cached = self._cache.get(key)
        if cached is not None and (now - cached[0]) < self.cache_ttl:
            return cached[1]
        checks = self._fetch(repo, number)
        self._cache[key] = (now, checks)
        return checks

    def invalidate(self, repo: str | None = None, number: int | None = None) -> None:
        """Drop cached entries — pass nothing to clear everything."""
        if repo is None and number is None:
            self._cache.clear()
            return
        for key in list(self._cache):
            if repo is not None and key[0] != repo:
                continue
            if number is not None and key[1] != number:
                continue
            del self._cache[key]

    # ── Internal ────────────────────────────────────────────────────────────

    def _fetch(self, repo: str, number: int) -> list[CheckRun]:
        try:
            result = subprocess.run(
                [
                    "gh", "pr", "checks", str(number),
                    "--repo", repo,
                    "--json", "name,state,conclusion,link,startedAt,completedAt",
                ],
                capture_output=True, text=True, timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []
        if result.returncode != 0:
            # `gh pr checks` exits non-zero when any check has failed — but
            # the JSON output on stdout is still valid in that case. Only
            # treat empty stdout as a real lookup failure.
            stdout = (result.stdout or "").strip()
            if not stdout:
                return []
        try:
            raw = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(raw, list):
            return []
        return [
            CheckRun(
                name=str(entry.get("name", "")),
                status=_normalize_status(str(entry.get("state", ""))),
                conclusion=_normalize_conclusion(entry.get("conclusion")),
                url=str(entry.get("link", "")),
                run_id=str(entry.get("link", "")).rstrip("/").rsplit("/", 1)[-1],
                started_at=_parse_ts(entry.get("startedAt")),
                completed_at=_parse_ts(entry.get("completedAt")),
            )
            for entry in raw
            if isinstance(entry, dict)
        ]
