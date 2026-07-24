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
  ``is_epic_issue``).

Fail-open by construction: a pair that has never been refreshed yields
``[]`` / ``False``, exactly the degraded values the live gates already
produce on a ``gh`` failure — so a fresh daemon serves a correct (if
CI-unannotated) board instantly instead of blocking on GitHub.

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


@dataclass(frozen=True)
class GateSnapshot:
    """Immutable, atomically-swapped view of the last gate refresh.

    Safe to hand to concurrent ``/board`` builds: the refresher never mutates
    a published snapshot, it swaps in a new one.
    """

    checks: dict[tuple[str, int], list[CheckRun]] = field(default_factory=dict)
    commit_messages: dict[tuple[str, int], list[str]] = field(default_factory=dict)
    epic_issues: dict[tuple[str, int], bool] = field(default_factory=dict)
    ci_available: bool = False
    refreshed_at: float | None = None

    # ── CiStore protocol ────────────────────────────────────────────────────
    def list_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]:
        return self.checks.get((repo, number), [])

    @property
    def is_available(self) -> bool:
        return self.ci_available

    # ── github_ops view consumed by merge_queue._entry_gate_status ─────────
    def get_pr_commit_messages(self, repo: str, number: int) -> list[str]:
        return self.commit_messages.get((repo, number), [])

    def is_epic_issue(self, repo: str, number: int) -> bool:
        return self.epic_issues.get((repo, number), False)


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
        entry with a PR, and atomically publishes a new snapshot.  Per-entry
        failures degrade that entry to the fail-open values; they never
        abort the pass or unpublish other entries' data.
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
            entries = [
                e for e in load_queue() if e.state == PENDING and e.pr_number
            ]
        except Exception:  # noqa: BLE001 — DB hiccup: keep serving the old snapshot
            log.warning("gate refresh: could not load merge queue", exc_info=True)
            return self._snapshot

        checks: dict[tuple[str, int], list[CheckRun]] = {}
        messages: dict[tuple[str, int], list[str]] = {}
        epics: dict[tuple[str, int], bool] = {}
        for entry in entries:
            key = (entry.repo_github, int(entry.pr_number))
            if ci_available:
                try:
                    checks[key] = inner.list_checks_for_pr(*key)
                except Exception:  # noqa: BLE001 — fail-open for this entry
                    checks[key] = []
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
            ci_available=ci_available,
            refreshed_at=time.time(),
        )
        self._snapshot = snap  # atomic publish
        return snap
