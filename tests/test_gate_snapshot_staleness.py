"""#1525: a published GateSnapshot must not gate CI on stale-green data.

`GateSnapshotRefresher.refresh` publishes a `GateSnapshot` stamped with
`refreshed_at`. Before this, `list_checks_for_pr` served whatever the last
refresh recorded no matter how old it was — indistinguishable, to any
consumer, from a genuinely fresh "no failing checks" read. These tests pin
the fail-closed contract added in `coord.gate_snapshot`: once a CI backend is
configured, a snapshot older than `STALE_AFTER_SECONDS` (or one that was
never refreshed at all) must read as a hard failure, not as silence.
"""

from __future__ import annotations

import time

from coord.ci_store import CheckRun, failed_checks
from coord.gate_snapshot import STALE_AFTER_SECONDS, GateSnapshot


def _passing_check() -> CheckRun:
    return CheckRun(
        name="pytest", status="completed", conclusion="success",
        url="", run_id="1", started_at=None, completed_at=None,
    )


class TestGateSnapshotStaleness:
    def test_fresh_snapshot_serves_real_checks(self) -> None:
        snap = GateSnapshot(
            checks={("acme/api", 7): [_passing_check()]},
            ci_available=True,
            refreshed_at=time.time(),
        )
        checks = snap.list_checks_for_pr("acme/api", 7)
        assert [c.name for c in checks] == ["pytest"]
        assert failed_checks(checks) == []

    def test_stale_snapshot_fails_closed(self) -> None:
        """A snapshot older than STALE_AFTER_SECONDS must not serve its
        last-known (possibly long out of date) green data."""
        snap = GateSnapshot(
            checks={("acme/api", 7): [_passing_check()]},
            ci_available=True,
            refreshed_at=time.time() - (STALE_AFTER_SECONDS + 1),
        )
        checks = snap.list_checks_for_pr("acme/api", 7)
        assert failed_checks(checks) == checks
        assert len(checks) == 1
        assert checks[0].conclusion == "unknown"

    def test_just_within_bound_serves_real_checks(self) -> None:
        snap = GateSnapshot(
            checks={("acme/api", 7): [_passing_check()]},
            ci_available=True,
            refreshed_at=time.time() - (STALE_AFTER_SECONDS - 5),
        )
        checks = snap.list_checks_for_pr("acme/api", 7)
        assert [c.name for c in checks] == ["pytest"]

    def test_never_refreshed_but_ci_available_fails_closed(self) -> None:
        """Shouldn't be reachable via the real refresh() flow (ci_available
        and refreshed_at are always set together), but the read path must
        not silently serve `[]` for this combination either."""
        snap = GateSnapshot(checks={}, ci_available=True, refreshed_at=None)
        checks = snap.list_checks_for_pr("acme/api", 7)
        assert failed_checks(checks) == checks
        assert len(checks) == 1

    def test_no_backend_configured_stays_fail_open(self) -> None:
        """ci_available=False (no CI backend, or type: none) has nothing to
        be stale about — untouched fail-open, matching NoOpCi."""
        snap = GateSnapshot()
        assert snap.list_checks_for_pr("acme/api", 7) == []
        assert snap.is_available is False


class TestGateSnapshotExpectsChecks:
    """#1904: `GateSnapshot.expects_checks` — the per-repo cache `refresh()`
    populates from the inner `CiStore` so the board/`--plan` read path can
    tell "no CI configured" apart from "CI exists but never triggered" the
    same way the live merge gate does."""

    def test_true_when_repo_cached_as_declaring_ci(self) -> None:
        snap = GateSnapshot(
            workflows_declared={"acme/api": True}, ci_available=True,
        )
        assert snap.expects_checks("acme/api", 7) is True

    def test_false_when_repo_cached_as_not_declaring_ci(self) -> None:
        snap = GateSnapshot(
            workflows_declared={"acme/api": False}, ci_available=True,
        )
        assert snap.expects_checks("acme/api", 7) is False

    def test_uncached_repo_fails_open(self) -> None:
        """Unlike `list_checks_for_pr`'s "unknown reads as failing" (#1525)
        posture, a repo this snapshot hasn't cached an answer for yet —
        never refreshed, or a fresh daemon boot before the first tick —
        reads as `False` (not `checks_absent`). This mirrors the module's
        documented "fail-open by construction" tradeoff: a fresh daemon
        serves an unannotated board instantly rather than reading every
        pending entry as untested-and-blocked before any I/O has happened."""
        snap = GateSnapshot(workflows_declared={}, ci_available=True)
        assert snap.expects_checks("acme/api", 7) is False

    def test_no_backend_configured_fails_open(self) -> None:
        snap = GateSnapshot()
        assert snap.expects_checks("acme/api", 7) is False
