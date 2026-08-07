"""Tick-refreshed snapshot of the third-party merge-gate inputs (#1336).

**Invariant 1 of the /board read path: read endpoints perform no third-party
I/O.**  Before this module, every cold ``GET /board`` build paid live ``gh``
subprocess calls — ``gh pr checks`` per pending merge-queue entry (via a
:class:`coord.ci_github.GitHubCi` constructed *per request*, so its cache
never survived) plus ``gh pr view --json commits`` and ``gh issue view`` per
entry for the #1318 epic-closing-keyword gate.  Board latency was therefore a
function of GitHub's latency × the number of open PRs — the root mechanism of
the #762/#715/#1336 timeout-overrun failure class.

Now the daemon's tick loop calls :meth:`GateSnapshotRefresher.refresh` on its
own cadence; the ``/board`` handler consumes the immutable
:class:`GateSnapshot` it last produced.  The snapshot duck-types both
consumer seams:

* the :class:`coord.ci_store.CiStore` protocol (``list_checks_for_pr`` /
  ``is_available``) — consumed by ``merge_queue.plan`` and
  ``stage_projection``;
* the two ``coord.github_ops`` functions ``merge_queue._entry_gate_status``
  reads for the epic-closing gate (``get_pr_commit_messages`` /
  ``is_epic_issue``);
* the two ``coord.github_ops`` functions the #821/#1475/#1479 review- and
  smoke-freshness checks read (``get_branch_sha`` / ``get_branch_patch_id``)
  — added by #1640, see below.

#1640: the snapshot used to duck-type only the first two seams, and
``merge_queue.has_smoke_verdict`` wraps each of its ``gh_ops`` lookups in a
fail-open ``except Exception``.  Handing it a snapshot that had no
``get_branch_sha`` therefore did not fail loudly — the ``AttributeError`` was
swallowed and *every* staleness check silently degraded to a no-op.  The
result was the exact "two readers, one truth" split #1640 reports:
``coord merge --plan`` (served from ``/board``, i.e. this snapshot) printed
READY for an entry that ``coord merge --only`` (live ``github_ops``, which
does resolve the SHAs) correctly refused as stale.  The lookups are served
here from tick-refreshed data so the read path still performs no third-party
I/O, and the two readers now apply the identical #1479 binding.

Fail-open by construction for a pair that has *never* been refreshed at all
(no backend configured yet, ``ci_available=False``): ``commit_messages`` /
``epic_issues`` still yield ``[]`` / ``False`` in that case, and
``list_checks_for_pr`` yields ``[]`` too — so a fresh daemon serves a correct
(if CI-unannotated) board instantly instead of blocking on GitHub.

Once a CI backend *is* configured, ``list_checks_for_pr`` is no longer
allowed to go stale silently (#1525): a snapshot older than
:data:`STALE_AFTER_SECONDS`, or one whose refresh loop has stalled entirely,
returns a synthetic failing check rather than the last-known (possibly
long-stale) green data. A stale-green board display was never the actual
mechanism behind the #1525 incident — ``coord merge`` doesn't consult this
snapshot at all, see below — but an unattended ``coord drive`` reads exactly
this display before deciding whether a merge is worth attempting, so it gets
the same fail-closed treatment as the live gate.

The *live* merge execution path (``coord merge``, auto-drain) keeps its own
live ``CiStore`` — merging is a write and is allowed to pay for fresh truth;
only the read path serves from the snapshot.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from coord.ci_store import CheckRun, build_ci_store

log = logging.getLogger("coord.serve")

# #1525: how old a published snapshot can get before the CI-check read path
# stops trusting its "no failing checks" silence. The refresh loop's default
# cadence is 30s (COORD_GATE_REFRESH_INTERVAL); 180s is 6x that — enough
# headroom that one slow refresh pass doesn't flap the board between READY
# and BLOCKED, while still catching a refresher that has stalled or died.
# This only gates checks (the safety-critical CI read); commit_messages /
# epic_issues keep the original fail-open contract described in the module
# docstring — the #1318 epic-closing-keyword gate isn't a merge-safety gate
# in the same sense CI is.
#
# This only protects the ``/board`` *display* path — ``coord merge`` and
# auto-drain always build their own live :class:`coord.ci_store.CiStore`
# (see the module docstring), never this snapshot. But a stale-green board
# is exactly what an unattended ``coord drive`` looks at before deciding
# whether a merge is even worth attempting, so a display-only lie here is
# still worth refusing to tell.
STALE_AFTER_SECONDS = 180.0


def _stale_check(age_seconds: float | None) -> CheckRun:
    """Synthetic failing :class:`CheckRun` standing in for "snapshot too old
    to trust" — mirrors :func:`coord.ci_github._unreadable_check`.
    ``conclusion="unknown"`` is not in
    :data:`coord.ci_store._PASSING_CONCLUSIONS`, so ``failed_checks`` treats
    this like any other hard failure.
    """
    age_desc = "never refreshed" if age_seconds is None else f"{age_seconds:.0f}s old"
    return CheckRun(
        name=f"coord: gate snapshot stale ({age_desc}, max {STALE_AFTER_SECONDS:.0f}s)",
        status="completed",
        conclusion="unknown",
        url="",
        run_id="",
        started_at=None,
        completed_at=None,
    )


@dataclass(frozen=True)
class GateSnapshot:
    """Immutable, atomically-swapped view of the last gate refresh.

    Safe to hand to concurrent ``/board`` builds: the refresher never mutates
    a published snapshot, it swaps in a new one.
    """

    checks: dict[tuple[str, int], list[CheckRun]] = field(default_factory=dict)
    commit_messages: dict[tuple[str, int], list[str]] = field(default_factory=dict)
    epic_issues: dict[tuple[str, int], bool] = field(default_factory=dict)
    # #1640: (repo, branch) -> HEAD SHA, and (repo, base, head) -> patch-id,
    # for the #821/#1475/#1479 review/smoke freshness checks.  A key that is
    # absent (never refreshed, or the lookup failed) yields None, which those
    # checks already treat as "anchor unavailable → skip that half" — the
    # same fail-open convention they apply to a live `gh` call that errors.
    branch_shas: dict[tuple[str, str], str | None] = field(default_factory=dict)
    branch_patch_ids: dict[tuple[str, str, str], str | None] = field(
        default_factory=dict
    )
    # #1904: repo -> whether the inner CiStore believes this repo declares
    # CI at all — the signal `expects_checks` below needs to tell "no CI
    # configured" apart from "CI exists but never reported for this PR"
    # when `list_checks_for_pr` comes back empty. Keyed on repo (not
    # (repo, number)) since workflow declarations are repo-wide.
    workflows_declared: dict[str, bool] = field(default_factory=dict)
    ci_available: bool = False
    refreshed_at: float | None = None

    # ── CiStore protocol ────────────────────────────────────────────────────
    def list_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]:
        # #1525: once a CI backend is configured (`ci_available`), a snapshot
        # that's stale-beyond-bound must not silently read as "no failing
        # checks" — that's the same fail-open shape as an unreadable `gh`
        # call, just reached via a dead/slow refresh loop instead of a dead
        # `gh` process. `ci_available=False` (no backend configured) is left
        # alone: there's nothing to be stale about.
        if self.ci_available:
            age = None if self.refreshed_at is None else time.time() - self.refreshed_at
            if age is None or age > STALE_AFTER_SECONDS:
                return [_stale_check(age)]
        return self.checks.get((repo, number), [])

    def expects_checks(self, repo: str, number: int) -> bool:
        # #1904: unlike `list_checks_for_pr`'s "unknown reads as failing"
        # (#1525) posture, a repo this snapshot hasn't cached an answer for
        # yet — never refreshed, or `ci_available=False` — reads as `False`
        # (not "checks absent"). This mirrors the module docstring's
        # documented "fail-open by construction" tradeoff for a snapshot
        # that hasn't (yet) done any I/O: a fresh daemon boot serves an
        # unannotated board instantly rather than reading every pending
        # entry as untested-and-blocked before the first tick even runs.
        # Once `refresh()` has run at least once for this repo, the cached
        # answer is real GitHub truth, not a guess.
        return self.workflows_declared.get(repo, False)

    @property
    def is_available(self) -> bool:
        return self.ci_available

    # ── github_ops view consumed by merge_queue._entry_gate_status ─────────
    def get_pr_commit_messages(self, repo: str, number: int) -> list[str]:
        return self.commit_messages.get((repo, number), [])

    def is_epic_issue(self, repo: str, number: int) -> bool:
        return self.epic_issues.get((repo, number), False)

    # ── github_ops view consumed by the review/smoke freshness checks ──────
    # (#1640 — merge_queue.has_approved_review / evaluate_smoke_verdict)
    def get_branch_sha(self, repo: str, branch: str) -> str | None:
        return self.branch_shas.get((repo, branch))

    def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
        return self.branch_patch_ids.get((repo, base, branch))


class GateSnapshotRefresher:
    """Owns the current :class:`GateSnapshot`; refreshed by the daemon tick.

    ``snapshot()`` is what the read path consumes — a bare attribute read
    (atomic under CPython), never I/O.  ``refresh(config)`` is the only
    method that talks to GitHub and must only ever run from the daemon's
    tick machinery (or a test driving it explicitly).
    """

    def __init__(self) -> None:
        self._snapshot = GateSnapshot()
        self._ci_type: str | None = None
        self._inner_ci = None  # CiStore | None — rebuilt when config type changes

    def snapshot(self) -> GateSnapshot:
        return self._snapshot

    # ── the tick-side refresh (the ONLY third-party I/O) ────────────────────
    def refresh(self, config) -> GateSnapshot:  # noqa: ANN001 — coord.config.Config
        """One refresh pass over the pending merge-queue entries.

        Reads the queue from the local DB, fetches CI checks + PR commit
        messages (+ epic-ness of any closing-keyword targets) per pending
        entry with a PR, plus (#1640) the branch/base HEAD SHAs and the
        branch's patch-id for every pending entry, and atomically publishes a
        new snapshot.  Per-entry failures degrade that entry to the fail-open
        values; they never abort the pass or unpublish other entries' data.

        Cost note (#1640): the SHA sweep adds up to two ``gh api
        repos/…/branches/…`` calls and one ``gh api compare`` per pending
        entry per pass.  Branch SHAs are deduped across entries, so a group
        of N entries sharing one target branch pays for that base once.  The
        merge queue's *pending* set is what bounds this — merged history is
        never refreshed — so it stays proportional to work actually waiting
        to merge, not to project age.
        """
        from coord import github_ops  # noqa: PLC0415
        from coord.merge_queue import PENDING, load_queue  # noqa: PLC0415
        from coord.pr_body_lint import find_closing_references  # noqa: PLC0415

        ci_type = getattr(getattr(config, "ci_store", None), "type", "none")
        if ci_type != self._ci_type:
            self._ci_type = ci_type
            try:
                self._inner_ci = build_ci_store(ci_type)
            except Exception:  # noqa: BLE001 — unknown type: disable the CI gate
                self._inner_ci = None
        inner = self._inner_ci
        ci_available = bool(inner is not None and inner.is_available)

        try:
            # #1640: the SHA/patch-id sweep covers every PENDING entry, not
            # just the ones with a PR — a freshly-enqueued entry has no PR
            # yet but is exactly the one `coord merge --plan` renders, and
            # leaving it out is what let the plan show READY for a verdict
            # the live gate rejects as stale.
            pending = [e for e in load_queue() if e.state == PENDING]
        except Exception:  # noqa: BLE001 — DB hiccup: keep serving the old snapshot
            log.warning("gate refresh: could not load merge queue", exc_info=True)
            return self._snapshot
        entries = [e for e in pending if e.pr_number]

        checks: dict[tuple[str, int], list[CheckRun]] = {}
        messages: dict[tuple[str, int], list[str]] = {}
        epics: dict[tuple[str, int], bool] = {}
        branch_shas: dict[tuple[str, str], str | None] = {}
        branch_patch_ids: dict[tuple[str, str, str], str | None] = {}
        workflows_declared: dict[str, bool] = {}
        for entry in pending:
            # Branch HEAD + merge-base HEAD + the branch's patch-id against
            # that base — the three anchors #821/#1475/#1479 compare a
            # recorded review/test verdict against.  Per-lookup failures are
            # cached as None (fail open for that half of the check), exactly
            # as the live path treats a `gh` error.
            for repo, branch in (
                (entry.repo_github, entry.branch),
                (entry.repo_github, entry.target_branch),
            ):
                if not repo or not branch or (repo, branch) in branch_shas:
                    continue
                try:
                    branch_shas[(repo, branch)] = github_ops.get_branch_sha(repo, branch)
                except Exception:  # noqa: BLE001 — fail-open for this branch
                    branch_shas[(repo, branch)] = None
            pid_key = (entry.repo_github, entry.target_branch, entry.branch)
            if all(pid_key) and pid_key not in branch_patch_ids:
                try:
                    branch_patch_ids[pid_key] = github_ops.get_branch_patch_id(*pid_key)
                except Exception:  # noqa: BLE001 — fail-open for this entry
                    branch_patch_ids[pid_key] = None

        for entry in entries:
            key = (entry.repo_github, int(entry.pr_number))
            if ci_available:
                try:
                    checks[key] = inner.list_checks_for_pr(*key)
                except Exception:  # noqa: BLE001 — fail-open for this entry
                    checks[key] = []
                # #1904: repo-wide, so dedupe across every pending entry in
                # the same repo — one `gh api .../actions/workflows` call
                # per repo per tick, not one per PR. A failure here reads
                # as `False` (not "checks absent") for this snapshot cycle
                # — see `expects_checks`' docstring for why the *display*
                # path stays fail-open where the live gate fails closed.
                if entry.repo_github not in workflows_declared:
                    try:
                        workflows_declared[entry.repo_github] = inner.expects_checks(*key)
                    except Exception:  # noqa: BLE001 — fail-open for this repo
                        workflows_declared[entry.repo_github] = False
            try:
                msgs = github_ops.get_pr_commit_messages(*key)
            except Exception:  # noqa: BLE001
                msgs = []
            messages[key] = msgs
            referenced: set[int] = set()
            for message in msgs:
                referenced.update(find_closing_references(message))
            for n in sorted(referenced):
                epic_key = (entry.repo_github, n)
                if epic_key in epics:
                    continue
                try:
                    epics[epic_key] = github_ops.is_epic_issue(*epic_key)
                except Exception:  # noqa: BLE001 — fail-open, matches the live gate
                    epics[epic_key] = False

        snap = GateSnapshot(
            checks=checks,
            commit_messages=messages,
            epic_issues=epics,
            branch_shas=branch_shas,
            branch_patch_ids=branch_patch_ids,
            workflows_declared=workflows_declared,
            ci_available=ci_available,
            refreshed_at=time.time(),
        )
        self._snapshot = snap  # atomic publish
        return snap
