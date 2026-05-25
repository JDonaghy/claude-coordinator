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


GH_SAMPLE = json.dumps([
    {
        "name": "test (3.12)",
        "state": "COMPLETED",
        "conclusion": "FAILURE",
        "link": "https://github.com/acme/api/actions/runs/123/job/456",
        "startedAt": "2026-05-24T12:00:00Z",
        "completedAt": "2026-05-24T12:05:00Z",
    },
    {
        "name": "lint",
        "state": "completed",
        "conclusion": "success",
        "link": "",
        "startedAt": "",
        "completedAt": "",
    },
    {
        "name": "deploy-preview",
        "state": "IN_PROGRESS",
        "conclusion": "",
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
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", side_effect=FileNotFoundError):
            checks = store.list_checks_for_pr("acme/api", 42)
        assert checks == []

    def test_handles_timeout(self) -> None:
        store = GitHubCi()
        with patch(
            "coord.ci_github.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
        ):
            checks = store.list_checks_for_pr("acme/api", 42)
        assert checks == []

    def test_handles_invalid_json(self) -> None:
        store = GitHubCi()
        with patch(
            "coord.ci_github.subprocess.run",
            return_value=_gh_result("not json", returncode=0),
        ):
            checks = store.list_checks_for_pr("acme/api", 42)
        assert checks == []

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
