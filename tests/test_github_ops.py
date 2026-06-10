"""Tests for coord/github_ops.py terminal-state helpers (#522).

``issue_is_closed`` and ``pr_is_merged`` are the GitHub-state guards the
auto-loop consults before dispatching a fix/re-review.  Both are best-effort
and **fail-open** — any ``gh`` error must resolve to ``False`` so a transient
failure never blocks a legitimate dispatch.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from coord import github_ops

# Captured at import time — the real function object, immune to the conftest
# autouse `_non_terminal_work` stub which reassigns the module attribute.
_REAL_WORK_IS_TERMINAL = github_ops.work_is_terminal


class TestIssueIsClosed:
    def test_true_when_state_closed(self) -> None:
        with patch(
            "coord.github_ops._gh",
            return_value=json.dumps({"number": 1, "state": "CLOSED"}),
        ):
            assert github_ops.issue_is_closed("acme/api", 1) is True

    def test_false_when_state_open(self) -> None:
        with patch(
            "coord.github_ops._gh",
            return_value=json.dumps({"number": 1, "state": "OPEN"}),
        ):
            assert github_ops.issue_is_closed("acme/api", 1) is False

    def test_fail_open_on_gh_error(self) -> None:
        with patch("coord.github_ops._gh", side_effect=RuntimeError("gh boom")):
            assert github_ops.issue_is_closed("acme/api", 1) is False

    def test_fail_open_on_malformed_json(self) -> None:
        with patch("coord.github_ops._gh", return_value="not json"):
            assert github_ops.issue_is_closed("acme/api", 1) is False


class TestPrIsMerged:
    def test_true_when_merged_at_present(self) -> None:
        payload = json.dumps(
            [{"number": 42, "state": "MERGED", "mergedAt": "2026-06-09T00:00:00Z"}]
        )
        with patch("coord.github_ops._gh", return_value=payload):
            assert github_ops.pr_is_merged("acme/api", "issue-1-fix") is True

    def test_true_when_state_merged_without_merged_at(self) -> None:
        payload = json.dumps([{"number": 42, "state": "MERGED", "mergedAt": None}])
        with patch("coord.github_ops._gh", return_value=payload):
            assert github_ops.pr_is_merged("acme/api", "issue-1-fix") is True

    def test_false_when_open(self) -> None:
        payload = json.dumps([{"number": 42, "state": "OPEN", "mergedAt": None}])
        with patch("coord.github_ops._gh", return_value=payload):
            assert github_ops.pr_is_merged("acme/api", "issue-1-fix") is False

    def test_false_when_no_pr_for_branch(self) -> None:
        with patch("coord.github_ops._gh", return_value="[]"):
            assert github_ops.pr_is_merged("acme/api", "issue-1-fix") is False

    def test_empty_branch_short_circuits_without_calling_gh(self) -> None:
        with patch(
            "coord.github_ops._gh",
            side_effect=AssertionError("gh must not be called for empty branch"),
        ):
            assert github_ops.pr_is_merged("acme/api", "") is False

    def test_fail_open_on_gh_error(self) -> None:
        with patch("coord.github_ops._gh", side_effect=RuntimeError("gh boom")):
            assert github_ops.pr_is_merged("acme/api", "issue-1-fix") is False

    def test_fail_open_on_malformed_json(self) -> None:
        with patch("coord.github_ops._gh", return_value="not json"):
            assert github_ops.pr_is_merged("acme/api", "issue-1-fix") is False


class TestWorkIsTerminal:
    """The #522 chokepoint guard: terminal == issue closed OR PR merged.

    Calls the captured real function (`_REAL_WORK_IS_TERMINAL`) so the conftest
    autouse non-terminal stub doesn't shadow it.  Patches the leaf helpers
    (`issue_is_closed` / `pr_is_merged`) which the real function looks up as
    module globals at call time.
    """

    def test_true_when_issue_closed(self) -> None:
        with patch("coord.github_ops.issue_is_closed", return_value=True), \
             patch("coord.github_ops.pr_is_merged", return_value=False):
            assert _REAL_WORK_IS_TERMINAL("acme/api", 1, "issue-1-fix") is True

    def test_true_when_pr_merged_even_if_issue_open(self) -> None:
        with patch("coord.github_ops.issue_is_closed", return_value=False), \
             patch("coord.github_ops.pr_is_merged", return_value=True):
            assert _REAL_WORK_IS_TERMINAL("acme/api", 1, "issue-1-fix") is True

    def test_false_when_neither(self) -> None:
        with patch("coord.github_ops.issue_is_closed", return_value=False), \
             patch("coord.github_ops.pr_is_merged", return_value=False):
            assert _REAL_WORK_IS_TERMINAL("acme/api", 1, "issue-1-fix") is False

    def test_false_for_empty_repo_without_calling_helpers(self) -> None:
        with patch(
            "coord.github_ops.issue_is_closed",
            side_effect=AssertionError("must not check state for empty repo"),
        ):
            assert _REAL_WORK_IS_TERMINAL("", 1, "issue-1-fix") is False

    def test_cache_collapses_repeat_calls(self) -> None:
        # The #349 ×4 case: a shared cache means the same merged issue costs
        # ONE issue_is_closed lookup across many revisits, not one per call.
        calls = {"n": 0}

        def counting_closed(*a, **k):
            calls["n"] += 1
            return True

        cache: dict = {}
        with patch("coord.github_ops.issue_is_closed", counting_closed):
            for _ in range(4):
                assert _REAL_WORK_IS_TERMINAL(
                    "acme/api", 349, "issue-349-fix", cache=cache
                ) is True

        assert calls["n"] == 1, "shared cache must collapse repeat gh lookups"

    def test_distinct_keys_not_collapsed(self) -> None:
        # Different (repo, issue, branch) keys must each be checked once.
        calls = {"n": 0}

        def counting_closed(*a, **k):
            calls["n"] += 1
            return False

        cache: dict = {}
        with patch("coord.github_ops.issue_is_closed", counting_closed), \
             patch("coord.github_ops.pr_is_merged", return_value=False):
            _REAL_WORK_IS_TERMINAL("acme/api", 1, "b1", cache=cache)
            _REAL_WORK_IS_TERMINAL("acme/api", 2, "b2", cache=cache)

        assert calls["n"] == 2
