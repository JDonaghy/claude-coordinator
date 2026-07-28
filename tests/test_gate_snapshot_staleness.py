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
