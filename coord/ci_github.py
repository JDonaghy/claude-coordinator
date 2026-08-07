"""GitHub Actions backend for :mod:`coord.ci_store`.

Fetches via :func:`coord.github_ops.get_pr_checks` (``gh pr checks <number>
--repo <slug> --json …`` — #1483: the single ``gh`` sink lives in
``github_ops``, not here) and maps the response to
:class:`coord.ci_store.CheckRun`.  Results are cached per-(repo, number) for
``cache_ttl`` seconds so the merge gate (which may iterate over many PRs)
doesn't hammer ``gh`` — the cost of a stale read in the gate path is at most
one wasted retry, and the user will re-run ``coord merge`` anyway.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime

from coord import github_ops
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


# #1564: `gh pr checks --json` has no `conclusion` field, and its `state`
# field is a per-check *verdict* (SUCCESS/FAILURE/SKIPPED/...), not a
# lifecycle phase — feeding `state` through a QUEUED/IN_PROGRESS/COMPLETED
# normaliser made every check fall through to the "unknown → in_progress"
# branch forever, so `failed_checks()` (which only looks at `status ==
# "completed"` checks) never evaluated anything and the gate blocked every
# merge unconditionally. `bucket` is gh's own normalisation of `state` into
# pass / fail / pending / skipping / cancel and is exactly the lifecycle +
# verdict split CheckRun wants: `pending` is the only in-flight bucket,
# everything else is a completed verdict.
_BUCKET_CONCLUSIONS: dict[str, str] = {
    "pass": "success",
    "fail": "failure",
    "skipping": "skipped",
    "cancel": "cancelled",
}


# #1851: an Actions check `link` is either a *run* URL
# (".../actions/runs/{run_id}") or a *job* URL
# (".../actions/runs/{run_id}/job/{job_id}") depending on whether the check
# has a job breakdown yet. `gh run rerun` takes the *run* id specifically —
# taking the last path segment (the pre-#1851 behaviour of the `run_id` field
# below) silently grabs the job id off a job URL instead. Nothing before
# #1851 ever read `CheckRun.run_id` (see the Phase 1 header in
# `coord/ci_store.py`), so fixing the extraction here has no back-compat
# concern.
_RUN_ID_RE = re.compile(r"/runs/(\d+)")


def _run_id_from_link(link: str) -> str:
    """Extract the numeric Actions *run* id from a `gh pr checks` `link` URL.

    Returns ``""`` when *link* is empty or doesn't match the expected Actions
    URL shape (e.g. a third-party check with no GitHub Actions run behind
    it) — callers treat an empty run id as "not rerunnable".
    """
    match = _RUN_ID_RE.search(link or "")
    return match.group(1) if match else ""


def _normalize_bucket(bucket: str) -> str:
    """Lowercase/None-safe normalisation of gh's ``bucket`` field, shared by
    :func:`_status_from_bucket` and :func:`_conclusion_from_bucket` so the two
    don't each repeat the same ``(bucket or "").lower()`` guard."""
    return (bucket or "").lower()


def _status_from_bucket(bucket: str) -> str:
    """Map gh's ``bucket`` to the CheckRun lifecycle enum ("in_progress" or
    "completed" — gh's own `--json bucket` doc lists no other pending-like
    value, so anything other than "pending" is treated as decided)."""
    return "in_progress" if _normalize_bucket(bucket) == "pending" else "completed"


def _conclusion_from_bucket(bucket: str) -> str | None:
    """Map gh's ``bucket`` to a CheckRun conclusion.

    "pending" has no conclusion yet (status is in-flight, see
    :func:`_status_from_bucket`). Anything that isn't one of gh's
    documented buckets (pass/fail/pending/skipping/cancel — e.g. a future
    bucket value this code has never seen) maps to "unknown" rather than
    being silently treated as passing, mirroring #1525's fail-closed
    synthetic-unreadable-check conclusion.
    """
    b = _normalize_bucket(bucket)
    if b == "pending":
        return None
    return _BUCKET_CONCLUSIONS.get(b, "unknown")


@dataclass
class GitHubCi:
    """Shell out to ``gh pr checks`` and cache results briefly."""

    cache_ttl: float = 10.0
    _cache: dict[tuple[str, int], tuple[float, list[CheckRun]]] = field(default_factory=dict)
    # #1904: repo -> (fetched_at, expects_checks). Keyed by repo alone (not
    # (repo, number) like `_cache` above) — whether a repo declares Actions
    # workflows doesn't vary per PR, so every PR in the same repo shares one
    # cached answer instead of paying a `gh api .../actions/workflows` round
    # trip each.
    _workflow_cache: dict[str, tuple[float, bool]] = field(default_factory=dict)

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

    def expects_checks(self, repo: str, number: int) -> bool:
        """True when *repo* declares at least one GitHub Actions workflow (#1904).

        *number* is accepted only to satisfy :class:`coord.ci_store.CiStore`'s
        shape — workflow *definitions* are repo-wide, not per-PR, so the
        cache (see ``_workflow_cache`` above) is keyed on *repo* alone.

        Fails closed (returns ``True``) on any read failure — an
        unreadable/erroring ``gh api .../actions/workflows`` call means "we
        don't know if this repo has CI", and #1525's rule is that unknown
        must read as "checks were expected", not as the free pass that let
        an empty ``checks`` list merge untested code in the first place.
        """
        now = time.time()
        cached = self._workflow_cache.get(repo)
        if cached is not None and (now - cached[0]) < self.cache_ttl:
            return cached[1]
        result = self._fetch_expects_checks(repo)
        self._workflow_cache[repo] = (now, result)
        return result

    def _fetch_expects_checks(self, repo: str) -> bool:
        try:
            count = github_ops.get_repo_workflow_count(repo)
        except (FileNotFoundError, subprocess.TimeoutExpired, RuntimeError, ValueError):
            return True  # fail closed — see expects_checks' docstring
        return count > 0

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

    def rerun_for_pr(self, repo: str, number: int) -> bool:
        """Re-run every distinct Actions run behind *repo*#*number*'s current
        checks via ``gh run rerun`` (#1851).

        A PR's checks can span more than one workflow run — `test.yml` and
        `cargo-test.yml` in this repo, for instance — so this reruns every
        distinct ``run_id`` among the PR's checks, not just the first. Best
        effort throughout: a check with no readable run id (an empty ``link``,
        or a third-party check with no Actions run behind it at all) is
        skipped rather than failing the whole call, and any individual
        ``gh run rerun`` failure is logged into the return value rather than
        raised — this is the remedy side of a fail-closed *reading*
        (:func:`coord.ci_store.checks_are_stale`), not itself required to be
        fail-closed: a rerun that only partially succeeds still helps, but
        the caller must not be told it fully worked.

        Returns ``True`` only when at least one run id was found *and* every
        rerun call it issued exited zero. Returns ``False`` when there was
        nothing rerunnable (no checks, or none with a readable run id) or any
        rerun call failed. Invalidates this PR's cache entry on any success
        so the next :meth:`list_checks_for_pr` reflects the freshly-queued
        run instead of the briefly-cached pre-rerun snapshot.
        """
        checks = self.list_checks_for_pr(repo, number)
        run_ids = sorted({c.run_id for c in checks if c.run_id})
        if not run_ids:
            return False
        all_ok = True
        any_ok = False
        for run_id in run_ids:
            # #1483: route through the single `gh` sink instead of shelling
            # out here directly.
            if github_ops.rerun_workflow_run(repo, run_id):
                any_ok = True
            else:
                all_ok = False
        if any_ok:
            self.invalidate(repo, number)
        return all_ok and any_ok

    # ── Internal ────────────────────────────────────────────────────────────

    def _fetch(self, repo: str, number: int) -> list[CheckRun]:
        try:
            raw = github_ops.get_pr_checks(repo, number)
        except github_ops.GhTooOldForJsonChecks as e:
            # #1564 Addendum 2: caught *ahead of* the generic RuntimeError
            # branch below — a `gh` too old to support `pr checks --json` at
            # all is a known, fixable host misconfiguration (upgrade gh on
            # whichever host runs the merge gate), not an auth/network flake.
            # `str(e)` already carries the actionable host + version-floor
            # message built by `github_ops._gh_too_old_message`; surfacing it
            # through a distinctly-named synthetic check (rather than folding
            # it into `_unreadable_check`'s generic wording) means an operator
            # reading the merge refusal never has to guess which of the two
            # this was.
            return [_gh_too_old_check(repo, number, str(e))]
        except (FileNotFoundError, subprocess.TimeoutExpired, RuntimeError, ValueError) as e:
            # #1525: a `gh pr checks` read that outright failed (gh missing,
            # timeout, non-zero exit with no stdout, unparseable JSON) used
            # to return `[]` here — indistinguishable from "this PR genuinely
            # has no checks configured", which the merge gate treats as
            # clear to merge. That silent fail-open is the mechanism that let
            # PR #1521 merge 11 minutes after `test (3.12)` recorded FAILURE:
            # a transient read failure at exactly the wrong moment read as
            # "no failing checks" instead of "unknown". Return a synthetic
            # failing check instead so the gate blocks and says why; a caller
            # that genuinely wants "unknown" treated as clear must pass
            # `force_merge=True` explicitly.
            return [_unreadable_check(repo, number, str(e))]
        if not isinstance(raw, list):
            return [_unreadable_check(repo, number, "gh pr checks returned non-list JSON")]
        return [
            CheckRun(
                name=str(entry.get("name", "")),
                status=_status_from_bucket(str(entry.get("bucket", ""))),
                conclusion=_conclusion_from_bucket(str(entry.get("bucket", ""))),
                url=str(entry.get("link", "")),
                run_id=_run_id_from_link(str(entry.get("link", ""))),
                started_at=_parse_ts(entry.get("startedAt")),
                completed_at=_parse_ts(entry.get("completedAt")),
            )
            for entry in raw
            if isinstance(entry, dict)
        ]


def _unreadable_check(repo: str, number: int, detail: str) -> CheckRun:
    """Synthetic :class:`CheckRun` standing in for "could not read CI" (#1525).

    ``conclusion="unknown"`` is not in :data:`coord.ci_store._PASSING_CONCLUSIONS`,
    so ``failed_checks`` picks this up like any other hard failure — the
    merge gate blocks and the reason (surfaced via ``CheckRun.name``) tells
    the operator this was a read failure, not a real CI failure.
    """
    return CheckRun(
        name=f"coord: could not read CI status for {repo}#{number} ({detail})",
        status="completed",
        conclusion="unknown",
        url="",
        run_id="",
        started_at=None,
        completed_at=None,
    )


def _gh_too_old_check(repo: str, number: int, detail: str) -> CheckRun:
    """Synthetic :class:`CheckRun` for "gh is too old to support `pr checks
    --json` at all" (#1564 Addendum 2) — deliberately worded and named
    differently from :func:`_unreadable_check` so the merge gate's refusal
    is unambiguous about *which* of the two this is: a known, fixable host
    misconfiguration (wrong gh version on the host running the gate), not a
    generic/transient read failure (auth, network, rate-limit). ``detail``
    is :class:`coord.github_ops.GhTooOldForJsonChecks`'s message, which
    already names the offending host and the required gh version.

    Still ``conclusion="unknown"`` (not in
    :data:`coord.ci_store._PASSING_CONCLUSIONS`) so the gate still fails
    closed and blocks the merge — #1525's fail-closed rule is not weakened,
    only the diagnosis attached to the block is sharper.
    """
    return CheckRun(
        name=f"coord: gh is too old to read CI status for {repo}#{number} ({detail})",
        status="completed",
        conclusion="unknown",
        url="",
        run_id="",
        started_at=None,
        completed_at=None,
    )
