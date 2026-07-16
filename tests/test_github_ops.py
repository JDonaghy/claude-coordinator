"""Tests for coord/github_ops.py terminal-state helpers (#522).

``issue_is_closed`` and ``pr_is_merged`` are the GitHub-state guards the
auto-loop consults before dispatching a fix/re-review.  Both are best-effort
and **fail-open** — any ``gh`` error must resolve to ``False`` so a transient
failure never blocks a legitimate dispatch.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

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


def _gh_pr_and_branch(pr_list_json: str, branch_json: str | None):
    """Build a ``_gh`` ``side_effect`` that answers ``pr list`` with
    *pr_list_json* and ``api .../branches/<name>`` with *branch_json* (or
    raises ``RuntimeError`` when *branch_json* is ``None``, simulating a
    deleted/unresolvable branch — see :func:`coord.github_ops.get_branch_sha`).
    """

    def _dispatch(*args, **kwargs):
        if args and args[0] == "pr":
            return pr_list_json
        if branch_json is None:
            raise RuntimeError("gh api branches: 404 not found")
        return branch_json

    return _dispatch


class TestPrIsMerged:
    """#1150: a historical merge on a *reused* branch name must not be
    confused with "this branch's current commits are merged" —
    ``--fix-of``/``--rework-of``/``--force`` all legitimately continue on an
    existing branch. ``pr_is_merged`` now requires the branch's *current* tip
    (via ``get_branch_sha``) to match the merged PR's ``headRefOid``.
    """

    def test_true_when_current_tip_matches_merged_pr(self) -> None:
        """The exact commit now on the branch is what merged -> True."""
        pr_payload = json.dumps([
            {"number": 42, "state": "MERGED", "mergedAt": "2026-06-09T00:00:00Z",
             "headRefOid": "deadbeef"},
        ])
        branch_payload = json.dumps({"commit": {"sha": "deadbeef"}})
        with patch("coord.github_ops._gh", side_effect=_gh_pr_and_branch(pr_payload, branch_payload)):
            assert github_ops.pr_is_merged("acme/api", "issue-1-fix") is True

    def test_false_when_new_commits_pushed_after_historical_merge(self) -> None:
        """#1150 core case: branch reused (--fix-of/--force) after a prior
        merge, with new commits on top -> the current tip is NOT the SHA that
        merged, so this must NOT be reported as merged."""
        pr_payload = json.dumps([
            {"number": 42, "state": "MERGED", "mergedAt": "2026-06-09T00:00:00Z",
             "headRefOid": "oldsha1"},
        ])
        branch_payload = json.dumps({"commit": {"sha": "newsha2"}})
        with patch("coord.github_ops._gh", side_effect=_gh_pr_and_branch(pr_payload, branch_payload)):
            assert github_ops.pr_is_merged("acme/api", "issue-1-fix") is False

    def test_true_when_branch_deleted_after_merge(self) -> None:
        """Tip unresolvable AND the branch is positively confirmed gone (404,
        the common case: branch deleted post-merge) -> falls back to the
        pre-#1150 'any historical merge counts' behavior, since a deleted
        branch cannot have gained new commits since. The shared
        ``_gh_pr_and_branch`` stub raises a 404-shaped RuntimeError for any
        non-``pr`` call, which satisfies both the ``get_branch_sha`` lookup
        and the follow-up ``branch_exists_on_remote`` confirmation check."""
        pr_payload = json.dumps([
            {"number": 42, "state": "MERGED", "mergedAt": "2026-06-09T00:00:00Z",
             "headRefOid": "oldsha1"},
        ])
        with patch("coord.github_ops._gh", side_effect=_gh_pr_and_branch(pr_payload, None)):
            assert github_ops.pr_is_merged("acme/api", "issue-1-fix") is True

    def test_false_when_sha_lookup_fails_for_non_404_reason(self) -> None:
        """#1150 review: a transient gh/network failure that leaves the SHA
        unresolved must NOT be treated the same as 'branch confirmed gone'.
        Conflating the two reintroduces this issue's exact bug class under a
        transient-failure trigger -- a rate limit or auth blip at the wrong
        moment would read as 'already merged', and callers (reconcile's merge
        sweep, prune_stale_queue_entries) act on a single True reading by
        permanently marking live, unmerged work as done/deleting its queue
        entry. Only a positively-confirmed-absent branch (404) may fall back
        to trusting history; every other failure must fail open toward
        False, per this module's documented convention."""
        pr_payload = json.dumps([
            {"number": 42, "state": "MERGED", "mergedAt": "2026-06-09T00:00:00Z",
             "headRefOid": "oldsha1"},
        ])

        def _dispatch(*args, **kwargs):
            if args and args[0] == "pr":
                return pr_payload
            # Not a "not found" / 4xx signal -- a generic transient failure.
            raise RuntimeError("gh: connection timed out")

        with patch("coord.github_ops._gh", side_effect=_dispatch):
            assert github_ops.pr_is_merged("acme/api", "issue-1-fix") is False

    def test_true_when_merged_at_present(self) -> None:
        # Current tip matches the merged PR's headRefOid, so the #1150
        # commit-aware check passes on its own merits (not via the
        # unresolvable-SHA fallback).
        pr_payload = json.dumps([
            {"number": 42, "state": "MERGED", "mergedAt": "2026-06-09T00:00:00Z",
             "headRefOid": "deadbeef"},
        ])
        branch_payload = json.dumps({"commit": {"sha": "deadbeef"}})
        with patch("coord.github_ops._gh", side_effect=_gh_pr_and_branch(pr_payload, branch_payload)):
            assert github_ops.pr_is_merged("acme/api", "issue-1-fix") is True

    def test_true_when_state_merged_without_merged_at(self) -> None:
        pr_payload = json.dumps([
            {"number": 42, "state": "MERGED", "mergedAt": None,
             "headRefOid": "deadbeef"},
        ])
        branch_payload = json.dumps({"commit": {"sha": "deadbeef"}})
        with patch("coord.github_ops._gh", side_effect=_gh_pr_and_branch(pr_payload, branch_payload)):
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


class TestGetIssue:
    """#1138 review: `enforce_oracle_readiness` derives the `oracle:exempt`
    escape hatch from `get_issue(...).get("labels")`. That silently always
    returned `[]` in production because `get_issue()`'s `--json` field list
    omitted `labels` — masked by tests that mocked `get_issue()` itself
    (handing back a `labels` key the real function never produced). These
    tests mock only `_gh` (the `gh` subprocess boundary) so the real
    `get_issue()` — field list included — is what's under test."""

    def test_json_field_list_requests_labels(self) -> None:
        with patch(
            "coord.github_ops._gh",
            return_value=json.dumps({
                "number": 1, "title": "t", "body": "b", "state": "OPEN",
                "milestone": None, "labels": [],
            }),
        ) as mock_gh:
            github_ops.get_issue("acme/api", 1)

        args = mock_gh.call_args.args
        assert args[0] == "issue" and args[1] == "view"
        json_fields = args[args.index("--json") + 1].split(",")
        assert "labels" in json_fields

    def test_returns_labels_from_real_gh_payload(self) -> None:
        with patch(
            "coord.github_ops._gh",
            return_value=json.dumps({
                "number": 1, "title": "t", "body": "b", "state": "OPEN",
                "milestone": {"number": 37},
                "labels": [{"name": "oracle:exempt"}, {"name": "coord"}],
            }),
        ):
            issue = github_ops.get_issue("acme/api", 1)

        label_names = [lbl.get("name", "") for lbl in issue.get("labels") or []]
        assert label_names == ["oracle:exempt", "coord"]


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


# ── close-invariant chokepoint (#1196) ──────────────────────────────────────

_EPIC_WITH_OPEN_CHILD = json.dumps({
    "number": 1041,
    "title": "Epic",
    "state": "open",
    "milestone": None,
    "labels": [],
    "body": "## Sub-issues\n- [ ] #1039\n- [x] #1040\n",
})

_EPIC_ALL_CHILDREN_CLOSED = json.dumps({
    "number": 1041,
    "title": "Epic",
    "state": "open",
    "milestone": None,
    "labels": [],
    "body": "## Sub-issues\n- [x] #1039\n- [x] #1040\n",
})

_REGULAR_ISSUE = json.dumps({
    "number": 42,
    "title": "Fix auth",
    "state": "open",
    "milestone": None,
    "labels": [],
    "body": "Just a regular issue, no checklist.",
})


class TestGetOpenChildren:
    def test_returns_open_children_only(self) -> None:
        with patch("coord.github_ops._gh", return_value=_EPIC_WITH_OPEN_CHILD):
            children = github_ops.get_open_children("acme/api", 1041)
        assert children == [{"number": 1039, "state": "open"}]

    def test_empty_when_all_children_closed(self) -> None:
        with patch("coord.github_ops._gh", return_value=_EPIC_ALL_CHILDREN_CLOSED):
            assert github_ops.get_open_children("acme/api", 1041) == []

    def test_empty_for_regular_issue(self) -> None:
        with patch("coord.github_ops._gh", return_value=_REGULAR_ISSUE):
            assert github_ops.get_open_children("acme/api", 42) == []

    def test_fails_open_on_gh_error(self) -> None:
        # A transient lookup failure must not permanently wedge every close
        # in the system — close_issue is the enforcement point, not this.
        with patch("coord.github_ops._gh", side_effect=RuntimeError("gh boom")):
            assert github_ops.get_open_children("acme/api", 1041) == []

    def test_fails_open_on_malformed_checklist(self) -> None:
        # #1195's own precedent (per-epic parse-failure isolation on
        # /board): a malformed `## Sub-issues` block on *this* issue must
        # not wedge closing some *other* well-formed one.
        malformed = json.dumps({
            "number": 1041, "title": "Epic", "state": "open", "milestone": None,
            "labels": [],
            "body": "## Sub-issues\n- [ ] #1039\n- [ ] #1039\n",  # duplicate
        })
        with patch("coord.github_ops._gh", return_value=malformed):
            assert github_ops.get_open_children("acme/api", 1041) == []

    def test_has_open_children_true_false(self) -> None:
        with patch("coord.github_ops._gh", return_value=_EPIC_WITH_OPEN_CHILD):
            assert github_ops.has_open_children("acme/api", 1041) is True
        with patch("coord.github_ops._gh", return_value=_EPIC_ALL_CHILDREN_CLOSED):
            assert github_ops.has_open_children("acme/api", 1041) is False


class _FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr


class TestCloseIssueGuard:
    # `close_issue`'s actual `gh issue close` call goes through raw
    # `subprocess.run`, not the `_gh()` helper (idempotency on "already
    # closed" needs the exit code + stderr, which `_gh()` doesn't expose) —
    # so the guard's `get_issue` lookup is mocked via `_gh`, and the close
    # call itself via `subprocess.run`.

    def test_refuses_close_with_open_children(self) -> None:
        with patch("coord.github_ops._gh", return_value=_EPIC_WITH_OPEN_CHILD), \
             patch(
                 "coord.github_ops.subprocess.run",
                 side_effect=AssertionError("must not attempt the close call"),
             ):
            with pytest.raises(github_ops.IssueHasOpenChildrenError, match=r"#1039"):
                github_ops.close_issue("acme/api", 1041)

    def test_force_overrides_the_guard(self) -> None:
        with patch(
            "coord.github_ops._gh",
            side_effect=AssertionError("force=True must skip the open-children lookup"),
        ) as mock_gh, patch(
            "coord.github_ops.subprocess.run", return_value=_FakeCompletedProcess(),
        ) as mock_run:
            github_ops.close_issue("acme/api", 1041, force=True)
        mock_gh.assert_not_called()
        mock_run.assert_called_once()
        assert mock_run.call_args.args[0] == [
            "gh", "issue", "close", "1041", "--repo", "acme/api",
        ]

    def test_regular_issue_closes_unchanged(self) -> None:
        # No regression for the common case: an issue with no children
        # closes exactly as before, guard lookup included.
        with patch("coord.github_ops._gh", return_value=_REGULAR_ISSUE), \
             patch(
                 "coord.github_ops.subprocess.run",
                 return_value=_FakeCompletedProcess(),
             ) as mock_run:
            github_ops.close_issue("acme/api", 42)
        assert mock_run.call_args.args[0] == [
            "gh", "issue", "close", "42", "--repo", "acme/api",
        ]

    def test_still_idempotent_on_already_closed(self) -> None:
        with patch("coord.github_ops._gh", return_value=_REGULAR_ISSUE), \
             patch(
                 "coord.github_ops.subprocess.run",
                 return_value=_FakeCompletedProcess(1, "GraphQL: Issue already closed"),
             ):
            github_ops.close_issue("acme/api", 42)  # must not raise


class TestPrBodyWrappers:
    def test_get_pr_body(self) -> None:
        with patch(
            "coord.github_ops._gh",
            return_value=json.dumps({"body": "Closes #99"}),
        ):
            assert github_ops.get_pr_body("acme/api", 5) == "Closes #99"

    def test_get_pr_body_missing_field(self) -> None:
        with patch("coord.github_ops._gh", return_value=json.dumps({})):
            assert github_ops.get_pr_body("acme/api", 5) == ""

    def test_edit_pr_body(self) -> None:
        calls: list[tuple] = []
        with patch("coord.github_ops._gh", lambda *a, **k: calls.append(a) or ""):
            github_ops.edit_pr_body("acme/api", 5, "Refs #99")
        assert calls == [("pr", "edit", "5", "--repo", "acme/api", "--body", "Refs #99")]
