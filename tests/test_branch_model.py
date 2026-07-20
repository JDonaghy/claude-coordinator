"""Unit tests for `coord/branch_model.py` (#934, docs/PIPELINE_V2.md "Git
model") — the develop + feature-branch-per-milestone base-branch resolver.

The whole point of #934's design is that it's OPT-IN and fails open to
today's flat `default_branch` behavior: these tests pin that contract down
so a future change can't silently start routing non-opted-in repos through
a feature branch that doesn't exist.
"""

from __future__ import annotations

from coord.branch_model import (
    ensure_feature_branch_exists,
    feature_branch_name,
    fetch_issue_milestone_number,
    resolve_base_branch,
    resolve_base_branch_for_issue,
)
from coord.models import Repo


def _repo(**kw) -> Repo:
    return Repo(name="api", github="acme/api", **kw)


class TestFeatureBranchName:
    def test_format(self) -> None:
        assert feature_branch_name(42) == "feature/ms-42"


class TestResolveBaseBranch:
    def test_no_develop_branch_falls_back_to_default(self) -> None:
        """Repo never opted in — always default_branch, milestone or not."""
        repo = _repo(default_branch="main")
        assert resolve_base_branch(repo, 42) == "main"
        assert resolve_base_branch(repo, None) == "main"

    def test_develop_branch_but_no_milestone_falls_back_to_default(self) -> None:
        """Repo opted in, but this issue has no milestone — unaffected."""
        repo = _repo(default_branch="main", develop_branch="develop")
        assert resolve_base_branch(repo, None) == "main"

    def test_develop_branch_and_milestone_resolves_feature_branch(self) -> None:
        repo = _repo(default_branch="main", develop_branch="develop")
        assert resolve_base_branch(repo, 42) == "feature/ms-42"

    def test_default_branch_falls_back_to_main_when_unset(self) -> None:
        repo = _repo(default_branch="", develop_branch=None)
        assert resolve_base_branch(repo, None) == "main"

    def test_tolerates_repo_stand_in_missing_develop_branch_attr(self) -> None:
        """Some tests construct a minimal Repo-shaped stand-in that predates
        `develop_branch` — must fail open to default_branch, not AttributeError."""
        class _BareRepo:
            default_branch = "main"

        assert resolve_base_branch(_BareRepo(), 42) == "main"


class TestResolveBaseBranchForIssue:
    def test_derives_milestone_from_issue_dict(self) -> None:
        repo = _repo(default_branch="main", develop_branch="develop")
        issue_data = {"number": 7, "milestone": {"number": 9, "title": "M"}}
        assert resolve_base_branch_for_issue(repo, issue_data) == "feature/ms-9"

    def test_no_milestone_on_issue_falls_back(self) -> None:
        repo = _repo(default_branch="main", develop_branch="develop")
        issue_data = {"number": 7, "milestone": None}
        assert resolve_base_branch_for_issue(repo, issue_data) == "main"


class TestFetchIssueMilestoneNumber:
    def test_returns_milestone_number(self) -> None:
        calls = []

        def fake_get_issue(repo_github, issue_number):
            calls.append((repo_github, issue_number))
            return {"milestone": {"number": 5, "title": "M"}}

        import coord.branch_model as bm
        from unittest.mock import patch

        with patch.object(bm.github_ops, "get_issue", fake_get_issue):
            result = fetch_issue_milestone_number("acme/api", 7)
        assert result == 5
        assert calls == [("acme/api", 7)]

    def test_no_milestone_returns_none(self) -> None:
        import coord.branch_model as bm
        from unittest.mock import patch

        with patch.object(bm.github_ops, "get_issue", lambda *a: {"milestone": None}):
            assert fetch_issue_milestone_number("acme/api", 7) is None

    def test_fails_open_on_gh_error(self) -> None:
        import coord.branch_model as bm
        from unittest.mock import patch

        def boom(*a):
            raise RuntimeError("gh: not authenticated")

        with patch.object(bm.github_ops, "get_issue", boom):
            assert fetch_issue_milestone_number("acme/api", 7) is None

    def test_fails_open_on_gh_timeout(self) -> None:
        """Review finding #2: a `gh` hang past the 30s subprocess timeout
        raises subprocess.TimeoutExpired, NOT RuntimeError — the fail-open
        contract must cover this too, or one slow issue lookup aborts an
        entire dispatch/review/merge/reconcile batch pass."""
        import subprocess
        import coord.branch_model as bm
        from unittest.mock import patch

        def boom(*a):
            raise subprocess.TimeoutExpired(cmd="gh", timeout=30)

        with patch.object(bm.github_ops, "get_issue", boom):
            assert fetch_issue_milestone_number("acme/api", 7) is None

    def test_fails_open_on_malformed_json(self) -> None:
        """Review finding #2: truncated/malformed `gh` output raises
        json.JSONDecodeError from inside github_ops.get_issue — also not a
        RuntimeError, also must fail open."""
        import json
        import coord.branch_model as bm
        from unittest.mock import patch

        def boom(*a):
            raise json.JSONDecodeError("Expecting value", "", 0)

        with patch.object(bm.github_ops, "get_issue", boom):
            assert fetch_issue_milestone_number("acme/api", 7) is None

    def test_cache_avoids_second_call(self) -> None:
        calls = []

        def fake_get_issue(repo_github, issue_number):
            calls.append((repo_github, issue_number))
            return {"milestone": {"number": 5}}

        import coord.branch_model as bm
        from unittest.mock import patch

        cache: dict = {}
        with patch.object(bm.github_ops, "get_issue", fake_get_issue):
            fetch_issue_milestone_number("acme/api", 7, cache=cache)
            fetch_issue_milestone_number("acme/api", 7, cache=cache)
        assert len(calls) == 1


class TestEnsureFeatureBranchExists:
    def test_raises_when_repo_not_opted_in(self) -> None:
        repo = _repo(default_branch="main", develop_branch=None)
        try:
            ensure_feature_branch_exists(repo, 42)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass

    def test_noop_when_branch_already_exists(self) -> None:
        repo = _repo(default_branch="main", develop_branch="develop")
        created = []
        result = ensure_feature_branch_exists(
            repo, 42,
            exists=lambda gh, b: True,
            get_sha=lambda gh, b: (_ for _ in ()).throw(AssertionError("should not fetch sha")),
            create=lambda gh, b, sha: created.append((gh, b, sha)) or True,
        )
        assert result == "feature/ms-42"
        assert created == []

    def test_creates_branch_off_develop_when_missing(self) -> None:
        repo = _repo(default_branch="main", develop_branch="develop")
        created = []
        result = ensure_feature_branch_exists(
            repo, 42,
            exists=lambda gh, b: False,
            get_sha=lambda gh, b: "deadbeef" if b == "develop" else "wrong",
            create=lambda gh, b, sha: created.append((gh, b, sha)) or True,
        )
        assert result == "feature/ms-42"
        assert created == [("acme/api", "feature/ms-42", "deadbeef")]

    def test_raises_when_create_reports_failure(self) -> None:
        """Review finding #1: github_ops.create_remote_branch catches its
        own RuntimeError and returns False on failure rather than raising.
        ensure_feature_branch_exists must check that return value and raise
        itself — otherwise it silently returns a branch name that doesn't
        exist on the remote, and callers relying on `except (ValueError,
        RuntimeError)` (e.g. milestone_dispatch.dispatch_entry) never catch
        the failure."""
        repo = _repo(default_branch="main", develop_branch="develop")
        try:
            ensure_feature_branch_exists(
                repo, 42,
                exists=lambda gh, b: False,
                get_sha=lambda gh, b: "deadbeef",
                create=lambda gh, b, sha: False,
            )
            raise AssertionError("expected RuntimeError")
        except RuntimeError:
            pass
