"""Tests for coord.ci_store (Phase 1 of #240).

The unit tests cover:
- Protocol + NoOpCi behaviour
- Helpers: failed_checks / in_flight_checks / summarize
- GitHubCi field mapping and caching
- Merge gate integration: failed/pending check blocks merge; --force-merge overrides
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from coord.ci_github import GitHubCi
from coord.ci_store import (
    CheckRun,
    NoOpCi,
    build_ci_store,
    failed_checks,
    in_flight_checks,
    summarize,
)


# ── NoOpCi ───────────────────────────────────────────────────────────────────

class TestNoOpCi:
    def test_is_not_available(self) -> None:
        assert NoOpCi().is_available is False

    def test_returns_empty(self) -> None:
        assert NoOpCi().list_checks_for_pr("acme/api", 1) == []


# ── Helpers ──────────────────────────────────────────────────────────────────

def _check(name: str, status: str = "completed", conclusion: str | None = "success") -> CheckRun:
    return CheckRun(
        name=name, status=status, conclusion=conclusion,
        url=f"https://gh/runs/{name}", run_id=name,
        started_at=None, completed_at=None,
    )


class TestFailedChecks:
    def test_picks_failure(self) -> None:
        items = [_check("a"), _check("b", conclusion="failure"), _check("c")]
        assert [x.name for x in failed_checks(items)] == ["b"]

    def test_picks_cancelled_and_timed_out_and_action_required(self) -> None:
        items = [
            _check("a", conclusion="cancelled"),
            _check("b", conclusion="timed_out"),
            _check("c", conclusion="action_required"),
            _check("ok"),
        ]
        names = {x.name for x in failed_checks(items)}
        assert names == {"a", "b", "c"}

    def test_skipped_is_not_failed(self) -> None:
        assert failed_checks([_check("a", conclusion="skipped")]) == []

    def test_neutral_is_not_failed(self) -> None:
        assert failed_checks([_check("a", conclusion="neutral")]) == []

    def test_stale_is_failed(self) -> None:
        """#1525: allow-list, not deny-list — GitHub's `stale` conclusion
        (superseded by a newer run) wasn't in the old deny-list at all and
        would have silently passed."""
        assert [c.name for c in failed_checks([_check("a", conclusion="stale")])] == ["a"]

    def test_unrecognised_conclusion_is_failed(self) -> None:
        """#1525: a conclusion this codebase has never seen (a future GitHub
        addition, or the synthetic "unknown" ci_github.py emits on a read
        failure) must default to blocking, not passing."""
        assert [
            c.name for c in failed_checks([_check("a", conclusion="something_new")])
        ] == ["a"]

    def test_in_flight_check_is_not_failed(self) -> None:
        """A queued/running check has conclusion=None and must be classified
        by in_flight_checks, never counted as failed here."""
        items = [_check("a", status="in_progress", conclusion=None)]
        assert failed_checks(items) == []


class TestInFlightChecks:
    def test_picks_queued_and_running(self) -> None:
        items = [
            _check("a", status="queued", conclusion=None),
            _check("b", status="in_progress", conclusion=None),
            _check("c"),
        ]
        names = {x.name for x in in_flight_checks(items)}
        assert names == {"a", "b"}


class TestSummarize:
    def test_empty(self) -> None:
        assert summarize([]) == "no checks"

    def test_mixed(self) -> None:
        items = [
            _check("ok"),
            _check("bad", conclusion="failure"),
            _check("wip", status="in_progress", conclusion=None),
        ]
        s = summarize(items)
        assert "1✓" in s
        assert "1✗" in s
        assert "1⋯" in s


# ── build_ci_store ───────────────────────────────────────────────────────────

class TestBuildCiStore:
    def test_github(self) -> None:
        store = build_ci_store("github")
        assert isinstance(store, GitHubCi)
        assert store.is_available is True

    def test_none(self) -> None:
        store = build_ci_store("none")
        assert isinstance(store, NoOpCi)
        assert store.is_available is False

    def test_unknown_falls_back_to_noop(self) -> None:
        # A typo in coordinator.yml shouldn't crash the merge command.
        store = build_ci_store("buildkite-but-misspelled")
        assert isinstance(store, NoOpCi)


# ── GitHubCi backend (subprocess mocked) ─────────────────────────────────────

def _gh_result(stdout: str = "[]", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


# #1564: this is the *real* shape `gh pr checks --json name,state,bucket,
# link,startedAt,completedAt` returns — no `conclusion` field, and `state`
# is a verdict (SUCCESS/FAILURE/...), not a lifecycle phase. `bucket` is
# gh's own pass/fail/pending rollup and is what GitHubCi now keys off.
GH_SAMPLE = json.dumps([
    {
        "name": "test (3.12)",
        "state": "FAILURE",
        "bucket": "fail",
        "link": "https://github.com/acme/api/actions/runs/123/job/456",
        "startedAt": "2026-05-24T12:00:00Z",
        "completedAt": "2026-05-24T12:05:00Z",
    },
    {
        "name": "lint",
        "state": "SUCCESS",
        "bucket": "pass",
        "link": "",
        "startedAt": "",
        "completedAt": "",
    },
    {
        "name": "deploy-preview",
        "state": "PENDING",
        "bucket": "pending",
        "link": "https://github.com/acme/api/actions/runs/789",
        "startedAt": "2026-05-24T12:10:00Z",
        "completedAt": "",
    },
])


class TestGitHubCi:
    def test_maps_fields(self) -> None:
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(GH_SAMPLE)):
            checks = store.list_checks_for_pr("acme/api", 42)
        assert len(checks) == 3
        by_name = {c.name: c for c in checks}
        assert by_name["test (3.12)"].status == "completed"
        assert by_name["test (3.12)"].conclusion == "failure"
        assert by_name["test (3.12)"].url.endswith("/job/456")
        assert by_name["lint"].conclusion == "success"
        assert by_name["deploy-preview"].status == "in_progress"
        assert by_name["deploy-preview"].conclusion is None
        # Timestamps are parsed to floats when present.
        assert isinstance(by_name["test (3.12)"].started_at, float)
        assert by_name["lint"].started_at is None

    def test_real_gh_shape_all_pass_yields_zero_failed_and_zero_inflight(self) -> None:
        """#1564 addendum acceptance test: feed exactly the JSON shape a real
        `gh pr checks --json name,state,bucket,...` call returns for an
        all-green PR (no `conclusion` field at all) through GitHubCi and
        confirm it reads as green — the pre-fix code failed this on both
        counts (every check normalised to "in_progress" forever)."""
        payload = json.dumps([
            {
                "name": "test (3.13)", "state": "SUCCESS", "bucket": "pass",
                "link": "https://github.com/acme/api/actions/runs/1/job/1",
                "startedAt": "2026-07-28T00:00:00Z", "completedAt": "2026-07-28T00:01:00Z",
            },
            {
                "name": "e2e", "state": "SUCCESS", "bucket": "pass",
                "link": "https://github.com/acme/api/actions/runs/1/job/2",
                "startedAt": "2026-07-28T00:00:00Z", "completedAt": "2026-07-28T00:01:00Z",
            },
        ])
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(payload)):
            checks = store.list_checks_for_pr("acme/api", 1562)
        assert failed_checks(checks) == []
        assert in_flight_checks(checks) == []

    def test_bucket_maps_to_conclusion_and_status(self) -> None:
        """#1564: gh's documented buckets (pass/fail/pending/skipping/cancel)
        map to CheckRun's status/conclusion — this is the mapping the merge
        gate actually reads."""
        payload = json.dumps([
            {"name": "a", "state": "SUCCESS", "bucket": "pass",
             "link": "", "startedAt": "", "completedAt": ""},
            {"name": "b", "state": "FAILURE", "bucket": "fail",
             "link": "", "startedAt": "", "completedAt": ""},
            {"name": "c", "state": "SKIPPED", "bucket": "skipping",
             "link": "", "startedAt": "", "completedAt": ""},
            {"name": "d", "state": "CANCELLED", "bucket": "cancel",
             "link": "", "startedAt": "", "completedAt": ""},
            {"name": "e", "state": "PENDING", "bucket": "pending",
             "link": "", "startedAt": "", "completedAt": ""},
        ])
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(payload)):
            checks = store.list_checks_for_pr("acme/api", 1)
        by_name = {c.name: c for c in checks}
        assert (by_name["a"].status, by_name["a"].conclusion) == ("completed", "success")
        assert (by_name["b"].status, by_name["b"].conclusion) == ("completed", "failure")
        assert (by_name["c"].status, by_name["c"].conclusion) == ("completed", "skipped")
        assert (by_name["d"].status, by_name["d"].conclusion) == ("completed", "cancelled")
        assert (by_name["e"].status, by_name["e"].conclusion) == ("in_progress", None)

    def test_unrecognised_bucket_is_unknown_not_passing(self) -> None:
        """#1525's fail-closed rule extended to `bucket`: a future gh bucket
        value this code has never seen must not be silently read as passing."""
        payload = json.dumps([
            {"name": "weird", "state": "SOMETHING_NEW", "bucket": "mystery",
             "link": "", "startedAt": "", "completedAt": ""},
        ])
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(payload)):
            checks = store.list_checks_for_pr("acme/api", 1)
        assert checks[0].status == "completed"
        assert checks[0].conclusion == "unknown"
        assert failed_checks(checks) == checks

    def test_handles_failing_gh_with_valid_json(self) -> None:
        """gh exits non-zero when checks fail but stdout is still valid JSON."""
        store = GitHubCi()
        with patch(
            "coord.ci_github.subprocess.run",
            return_value=_gh_result(GH_SAMPLE, returncode=1),
        ):
            checks = store.list_checks_for_pr("acme/api", 42)
        assert len(checks) == 3

    def test_handles_missing_gh(self) -> None:
        # #1525: a read failure must fail CLOSED — a synthetic "unknown"
        # check, not an empty list indistinguishable from "no checks
        # configured". `failed_checks` must pick it up as a hard failure.
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", side_effect=FileNotFoundError):
            checks = store.list_checks_for_pr("acme/api", 42)
        assert len(checks) == 1
        assert checks[0].conclusion == "unknown"
        assert failed_checks(checks) == checks

    def test_handles_timeout(self) -> None:
        store = GitHubCi()
        with patch(
            "coord.ci_github.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
        ):
            checks = store.list_checks_for_pr("acme/api", 42)
        assert len(checks) == 1
        assert checks[0].conclusion == "unknown"
        assert failed_checks(checks) == checks

    def test_handles_invalid_json(self) -> None:
        store = GitHubCi()
        with patch(
            "coord.ci_github.subprocess.run",
            return_value=_gh_result("not json", returncode=0),
        ):
            checks = store.list_checks_for_pr("acme/api", 42)
        assert len(checks) == 1
        assert checks[0].conclusion == "unknown"
        assert failed_checks(checks) == checks

    def test_handles_non_list_json(self) -> None:
        """Valid JSON that isn't a list (e.g. an error object) also fails closed."""
        store = GitHubCi()
        with patch(
            "coord.ci_github.subprocess.run",
            return_value=_gh_result('{"error": "rate limited"}', returncode=0),
        ):
            checks = store.list_checks_for_pr("acme/api", 42)
        assert len(checks) == 1
        assert checks[0].conclusion == "unknown"

    def test_cache_avoids_second_call(self) -> None:
        store = GitHubCi(cache_ttl=60.0)
        with patch(
            "coord.ci_github.subprocess.run",
            return_value=_gh_result(GH_SAMPLE),
        ) as run:
            store.list_checks_for_pr("acme/api", 42)
            store.list_checks_for_pr("acme/api", 42)
        assert run.call_count == 1

    def test_cache_invalidate(self) -> None:
        store = GitHubCi(cache_ttl=60.0)
        with patch(
            "coord.ci_github.subprocess.run",
            return_value=_gh_result(GH_SAMPLE),
        ) as run:
            store.list_checks_for_pr("acme/api", 42)
            store.invalidate()
            store.list_checks_for_pr("acme/api", 42)
        assert run.call_count == 2

    def test_cache_keyed_per_pr(self) -> None:
        store = GitHubCi(cache_ttl=60.0)
        with patch(
            "coord.ci_github.subprocess.run",
            return_value=_gh_result(GH_SAMPLE),
        ) as run:
            store.list_checks_for_pr("acme/api", 42)
            store.list_checks_for_pr("acme/api", 43)  # different PR
        assert run.call_count == 2


# ── Merge gate integration ───────────────────────────────────────────────────

from dataclasses import dataclass, field as dataclass_field
from coord.merge_queue import MERGED, MERGING, PENDING, QueuedMerge, process


@dataclass
class FakeCi:
    """Stub CiStore that returns canned responses per PR number."""

    by_pr: dict[int, list[CheckRun]] = dataclass_field(default_factory=dict)
    is_available: bool = True

    def list_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]:
        return self.by_pr.get(number, [])


@dataclass
class FakeGh:
    next_pr: int = 100
    merge_calls: list[tuple[int, str]] = dataclass_field(default_factory=list)

    def create_pr(self, repo: str, *, base: str, head: str, title: str, body: str) -> dict:
        n = self.next_pr
        self.next_pr += 1
        return {"number": n, "url": f"https://gh/x/{n}", "existed": False}

    def get_pr_size(self, repo: str, number: int) -> int:
        return 10

    def merge_pr(self, repo: str, number: int, method: str = "rebase") -> tuple[bool, str]:
        self.merge_calls.append((number, method))
        return True, "merged"


def _entry(aid: str = "a") -> QueuedMerge:
    return QueuedMerge(
        assignment_id=aid,
        repo_name="api",
        repo_github="acme/api",
        branch=f"worker/{aid}",
        target_branch="main",
        issue_number=1,
        issue_title="t",
        state=PENDING,
    )


class TestMergeGate:
    def test_failed_check_blocks_merge(self) -> None:
        items = [_entry("a")]
        gh = FakeGh()
        ci = FakeCi(by_pr={100: [_check("ci", conclusion="failure")]})
        events = process(items, gh, ci_store=ci)
        assert gh.merge_calls == []
        assert items[0].state == PENDING
        kinds = [e.kind for e in events]
        assert "checks_failed" in kinds

    def test_pending_check_blocks_merge(self) -> None:
        items = [_entry("a")]
        gh = FakeGh()
        ci = FakeCi(by_pr={100: [_check("ci", status="in_progress", conclusion=None)]})
        events = process(items, gh, ci_store=ci)
        assert gh.merge_calls == []
        kinds = [e.kind for e in events]
        assert "checks_pending" in kinds

    def test_passing_checks_allow_merge(self) -> None:
        items = [_entry("a")]
        gh = FakeGh()
        ci = FakeCi(by_pr={100: [_check("ci", conclusion="success")]})
        process(items, gh, ci_store=ci)
        assert gh.merge_calls == [(100, "rebase")]
        assert items[0].state == MERGED

    def test_force_merge_overrides_failed(self) -> None:
        items = [_entry("a")]
        gh = FakeGh()
        ci = FakeCi(by_pr={100: [_check("ci", conclusion="failure")]})
        process(items, gh, ci_store=ci, force_merge=True)
        assert gh.merge_calls == [(100, "rebase")]
        assert items[0].state == MERGED

    def test_unreadable_ci_blocks_merge(self) -> None:
        """#1525 regression: a CI read that failed (represented here the same
        way GitHubCi._fetch represents it — a synthetic "unknown" check) must
        refuse to merge, exactly like a real CI failure."""
        items = [_entry("a")]
        gh = FakeGh()
        ci = FakeCi(by_pr={100: [_check("ci", conclusion="unknown")]})
        events = process(items, gh, ci_store=ci)
        assert gh.merge_calls == []
        assert items[0].state == PENDING
        kinds = [e.kind for e in events]
        assert "checks_failed" in kinds

    def test_cancelled_check_blocks_merge(self) -> None:
        """#1525 regression: CANCELLED (e.g. a fail-fast sibling of a real
        failure) must refuse to merge without --force-merge."""
        items = [_entry("a")]
        gh = FakeGh()
        ci = FakeCi(by_pr={100: [_check("ci", conclusion="cancelled")]})
        events = process(items, gh, ci_store=ci)
        assert gh.merge_calls == []
        kinds = [e.kind for e in events]
        assert "checks_failed" in kinds

    def test_force_merge_overrides_unreadable_ci(self) -> None:
        items = [_entry("a")]
        gh = FakeGh()
        ci = FakeCi(by_pr={100: [_check("ci", conclusion="unknown")]})
        process(items, gh, ci_store=ci, force_merge=True)
        assert gh.merge_calls == [(100, "rebase")]
        assert items[0].state == MERGED

    def test_noop_ci_allows_merge(self) -> None:
        items = [_entry("a")]
        gh = FakeGh()
        process(items, gh, ci_store=NoOpCi())
        assert gh.merge_calls == [(100, "rebase")]

    def test_no_ci_store_allows_merge(self) -> None:
        """Backwards-compat: callers that don't pass ci_store still work."""
        items = [_entry("a")]
        gh = FakeGh()
        process(items, gh)
        assert gh.merge_calls == [(100, "rebase")]

    def test_failed_check_halts_group_only(self) -> None:
        """A failed check on one PR shouldn't block PRs in other groups."""
        items = [
            _entry("a"),
            QueuedMerge(
                assignment_id="b",
                repo_name="ui",
                repo_github="acme/ui",
                branch="worker/b",
                target_branch="main",
                issue_number=2,
                issue_title="t",
                state=PENDING,
            ),
        ]
        gh = FakeGh()
        ci = FakeCi(by_pr={100: [_check("ci", conclusion="failure")]})
        process(items, gh, ci_store=ci)
        # `a` blocked, `b` (different repo group) merged
        merged_prs = [c[0] for c in gh.merge_calls]
        assert 100 not in merged_prs
        assert 101 in merged_prs


class TestMergeGateThroughGitHubCi:
    """#1564 acceptance: black-box through the *real* :class:`GitHubCi`
    backend (not the ``FakeCi`` stub above) with `gh`'s actual
    ``--json name,state,bucket,...`` shape — green merges, red refuses and
    names the failing check, and an unreachable ``gh`` refuses as
    "unavailable" rather than silently allowing the merge."""

    def test_green_allows_merge(self) -> None:
        items = [_entry("a")]
        gh = FakeGh()
        payload = json.dumps([
            {"name": "test (3.13)", "state": "SUCCESS", "bucket": "pass",
             "link": "", "startedAt": "", "completedAt": ""},
        ])
        ci = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(payload)):
            process(items, gh, ci_store=ci)
        assert gh.merge_calls == [(100, "rebase")]
        assert items[0].state == MERGED

    def test_red_refuses_and_names_failing_check(self) -> None:
        items = [_entry("a")]
        gh = FakeGh()
        payload = json.dumps([
            {"name": "test (3.13)", "state": "FAILURE", "bucket": "fail",
             "link": "https://github.com/acme/api/actions/runs/1/job/1",
             "startedAt": "", "completedAt": ""},
        ])
        ci = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(payload)):
            events = process(items, gh, ci_store=ci)
        assert gh.merge_calls == []
        assert items[0].state == PENDING
        failed_event = next(e for e in events if e.kind == "checks_failed")
        assert "test (3.13)" in failed_event.message
        assert "failure" in failed_event.message

    def test_unreachable_gh_refuses_as_unavailable(self) -> None:
        items = [_entry("a")]
        gh = FakeGh()
        ci = GitHubCi()
        with patch("coord.ci_github.subprocess.run", side_effect=FileNotFoundError):
            events = process(items, gh, ci_store=ci)
        assert gh.merge_calls == []
        assert items[0].state == PENDING
        failed_event = next(e for e in events if e.kind == "checks_failed")
        assert "could not read CI status" in failed_event.message


# ── Config ───────────────────────────────────────────────────────────────────

from coord.config import _parse_ci_store, ConfigError


class TestParseCiStore:
    def test_absent_defaults_to_github(self) -> None:
        cfg = _parse_ci_store(None)
        assert cfg.type == "github"

    def test_explicit_none(self) -> None:
        cfg = _parse_ci_store({"type": "none"})
        assert cfg.type == "none"

    def test_explicit_github(self) -> None:
        cfg = _parse_ci_store({"type": "github"})
        assert cfg.type == "github"

    def test_invalid_type_raises(self) -> None:
        with pytest.raises(ConfigError):
            _parse_ci_store({"type": "buildkite"})

    def test_non_mapping_raises(self) -> None:
        with pytest.raises(ConfigError):
            _parse_ci_store(["github"])
