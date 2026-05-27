"""Tests for #241: type="conflict-fix" auto-rebase on merge conflict.

Covers:
- Conflict classification (rebaseable / human / unknown)
- Briefing assembly
- Machine selection (prefer worker's machine, fallback to any idle)
- Dispatcher integration with the board
- Reconcile hook: conflict-fix done → merge entry re-enqueued
- Reconcile hook: conflict-fix failed → merge entry HUMAN_REQUIRED
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest

from coord.config import Config, ReviewsConfig
from coord.conflict_fix import (
    CONFLICT_FIX_SYSTEM_PROMPT,
    build_conflict_fix_briefing,
    dispatch_conflict_fix,
    pick_conflict_fix_machine,
)
from coord.merge_queue import (
    CONFLICT,
    HUMAN_REQUIRED,
    MERGED,
    PENDING,
    QueuedMerge,
    classify_conflict,
)
from coord.models import Assignment, Board, Machine, Repo


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def repo() -> Repo:
    return Repo(name="api", github="acme/api", default_branch="main", test_command="pytest")


@pytest.fixture
def two_machine_config(repo: Repo) -> Config:
    return Config(
        repos=[repo],
        machines=[
            Machine(
                name="laptop", host="laptop.tail",
                repos=["api"], repo_paths={"api": "/work/api"},
            ),
            Machine(
                name="server", host="server.tail",
                repos=["api"], repo_paths={"api": "/srv/api"},
            ),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=False),
    )


def _entry(*, error: str | None = "Merge conflict in foo.py") -> QueuedMerge:
    return QueuedMerge(
        assignment_id="abc123",
        repo_name="api",
        repo_github="acme/api",
        branch="issue-1-fix",
        target_branch="main",
        issue_number=1,
        issue_title="Fix the thing",
        state=CONFLICT,
        pr_number=42,
        pr_url="https://github.com/acme/api/pull/42",
        error=error,
    )


# ── Classification ──────────────────────────────────────────────────────────


class TestClassifyConflict:
    @pytest.mark.parametrize("msg", [
        "Merge conflict in src/foo.py",
        "merge conflict",
        "could not be rebased",
        "branch is not up to date with the base branch",
        "non-fast-forward update rejected",
        "PR is behind the base branch",
        # #276: the actual phrasing gh pr merge returns when base has moved.
        "Pull request #273 is not mergeable: the merge commit cannot be cleanly created.",
        "X PR is not mergeable",
    ])
    def test_rebaseable(self, msg: str) -> None:
        assert classify_conflict(msg) == "rebaseable"

    @pytest.mark.parametrize("msg", [
        "required status check 'ci' has not passed",
        "Required review required",
        "permission denied",
        "Pushes to this protected branch are restricted",
        "branch protection rule blocks force-push",
    ])
    def test_human(self, msg: str) -> None:
        assert classify_conflict(msg) == "human"

    def test_unknown(self) -> None:
        assert classify_conflict("some other error") == "unknown"
        assert classify_conflict("") == "unknown"
        assert classify_conflict(None) == "unknown"


# ── Briefing ────────────────────────────────────────────────────────────────


class TestBuildBriefing:
    def test_contains_steps(self) -> None:
        briefing = build_conflict_fix_briefing(
            entry=_entry(), repo_path="/work/api", test_command="pytest -x",
        )
        assert "git fetch origin" in briefing
        assert "git checkout issue-1-fix" in briefing
        assert "git pull --rebase origin main" in briefing
        assert "git push --force-with-lease origin issue-1-fix" in briefing
        assert "pytest -x" in briefing

    def test_includes_error_context(self) -> None:
        briefing = build_conflict_fix_briefing(
            entry=_entry(error="Merge conflict in api/models.py"),
            repo_path="/work/api",
            test_command=None,
        )
        assert "Merge conflict in api/models.py" in briefing

    def test_warns_against_semantic_conflicts(self) -> None:
        briefing = build_conflict_fix_briefing(
            entry=_entry(), repo_path="/work/api", test_command="pytest",
        )
        assert "semantic" in briefing.lower()
        assert "DO NOT" in briefing or "do not" in briefing.lower()

    def test_no_test_command_falls_back(self) -> None:
        briefing = build_conflict_fix_briefing(
            entry=_entry(), repo_path="/work/api", test_command=None,
        )
        assert "no test command configured" in briefing


# ── Machine selection ───────────────────────────────────────────────────────


class TestPickMachine:
    def test_prefers_worker_machine_when_idle(self, two_machine_config: Config) -> None:
        board = Board()
        machine = pick_conflict_fix_machine(
            "api", board, two_machine_config, prefer_machine="laptop",
        )
        assert machine is not None
        assert machine.name == "laptop"

    def test_falls_back_to_idle_when_preferred_busy(
        self, two_machine_config: Config,
    ) -> None:
        board = Board()
        board.active.append(Assignment(
            machine_name="laptop", repo_name="api", issue_number=99, issue_title="x",
            status="running",
        ))
        machine = pick_conflict_fix_machine(
            "api", board, two_machine_config, prefer_machine="laptop",
        )
        assert machine is not None
        assert machine.name == "server"

    def test_returns_none_when_no_machine_handles_repo(self) -> None:
        cfg = Config(
            repos=[Repo(name="api", github="a/b")],
            machines=[Machine(name="m", host="h", repos=["other"])],
        )
        assert pick_conflict_fix_machine("api", Board(), cfg) is None


# ── Dispatch ────────────────────────────────────────────────────────────────


class _FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class _FakeHTTPClient:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, *, json: dict, timeout: float) -> _FakeHTTPResponse:
        self.calls.append((url, json))
        return _FakeHTTPResponse(self._payload)


class TestDispatch:
    def test_appends_to_board_and_sends_payload(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        board = Board()
        client = _FakeHTTPClient({"id": "fix-id-1"})
        result = dispatch_conflict_fix(
            _entry(), board, two_machine_config,
            http_client=client, prefer_machine="laptop", now=99.0,
        )
        assert result is not None
        assert result.type == "conflict-fix"
        assert result.machine_name == "laptop"
        assert result.branch == "issue-1-fix"
        assert result.review_of_assignment_id == "abc123"
        assert result.dispatched_at == 99.0
        assert board.active == [result]

        assert len(client.calls) == 1
        url, payload = client.calls[0]
        assert "laptop.tail" in url
        assert payload["type"] == "conflict-fix"
        assert payload["system_prompt"] == CONFLICT_FIX_SYSTEM_PROMPT
        assert payload["review_target"] == "issue-1-fix"
        assert payload["branch"] == "issue-1-fix"
        assert payload["repo_path"] == "/work/api"

    def test_returns_none_if_already_in_flight(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        board = Board()
        board.active.append(Assignment(
            machine_name="server", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="prev-fix", status="running",
            type="conflict-fix", review_of_assignment_id="abc123",
        ))
        result = dispatch_conflict_fix(
            _entry(), board, two_machine_config,
            http_client=_FakeHTTPClient({"id": "would-not-fire"}),
        )
        assert result is None

    def test_returns_none_when_http_fails(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        import httpx

        class _Failing:
            def post(self, url, *, json, timeout):
                raise httpx.ConnectError("offline")

        result = dispatch_conflict_fix(
            _entry(), Board(), two_machine_config, http_client=_Failing(),
        )
        assert result is None

    def test_payload_includes_deny_commands(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """gh and force-push must be in the dispatch payload's deny_commands.

        Regression test for review of #243: CLAUDE.md claims `gh` is denied
        for conflict-fix workers but the payload had no deny_commands key.
        Enforcement was prompt-only; now it's enforced by the agent harness.
        """
        client = _FakeHTTPClient({"id": "fix-id-deny"})
        result = dispatch_conflict_fix(
            _entry(), Board(), two_machine_config,
            http_client=client, prefer_machine="laptop",
        )
        assert result is not None
        _, payload = client.calls[0]
        deny = payload.get("deny_commands", [])
        assert "Bash(gh *)" in deny
        assert "Bash(git push --force *)" in deny
        assert "Bash(git push -f *)" in deny

    def test_payload_deny_commands_merge_with_repo_config(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """Repo-level worker_permissions.deny entries are preserved alongside
        the conflict-fix-specific deny patterns (no clobbering)."""
        from coord.models import WorkerPermissionsConfig
        cfg = two_machine_config
        cfg.repos[0].worker_permissions = WorkerPermissionsConfig(
            deny=["Bash(rm -rf *)", "Bash(curl *)"],
        )
        client = _FakeHTTPClient({"id": "fix-id-merge"})
        dispatch_conflict_fix(
            _entry(), Board(), cfg, http_client=client, prefer_machine="laptop",
        )
        _, payload = client.calls[0]
        deny = payload["deny_commands"]
        assert "Bash(rm -rf *)" in deny
        assert "Bash(curl *)" in deny
        assert "Bash(gh *)" in deny
        assert "Bash(git push --force *)" in deny
        # Dedup: no repeats even if repo config happened to include one.
        assert len(deny) == len(set(deny))

    def test_payload_pins_target_branch_to_original(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """#277: the dispatch payload must set ``target_branch`` to the
        original branch so the agent checks out that branch instead of
        deriving an orphan slug from ``[conflict-fix] <title>``."""
        client = _FakeHTTPClient({"id": "fix-id-tb"})
        dispatch_conflict_fix(
            _entry(), Board(), two_machine_config,
            http_client=client, prefer_machine="laptop",
        )
        _, payload = client.calls[0]
        assert payload.get("target_branch") == _entry().branch

    def test_retry_cap_blocks_second_dispatch_when_prior_completed(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """A conflict-fix in ``board.completed`` must block a second dispatch.

        Without this guard the dispatcher would re-fire after every
        ``coord merge`` retry, because :func:`coord.claim.has_active_followup`
        only scans ``board.active`` — the original bug from the review of
        #243.
        """
        board = Board()
        board.completed.append(Assignment(
            machine_name="server", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="prev-fix", status="done",
            type="conflict-fix", review_of_assignment_id="abc123",
        ))
        client = _FakeHTTPClient({"id": "would-not-fire"})
        result = dispatch_conflict_fix(
            _entry(), board, two_machine_config, http_client=client,
        )
        assert result is None
        assert client.calls == [], "HTTP should not be called when retry cap hit"


class TestHasPriorConflictFix:
    """Cover the retry-cap predicate directly so the cli.py guard is exercised."""

    def test_false_on_empty_board(self) -> None:
        from coord.conflict_fix import has_prior_conflict_fix
        assert has_prior_conflict_fix(Board(), "abc123") is False

    def test_false_when_assignment_id_is_none(self) -> None:
        from coord.conflict_fix import has_prior_conflict_fix
        assert has_prior_conflict_fix(Board(), None) is False

    def test_true_when_active_has_matching_conflict_fix(self) -> None:
        from coord.conflict_fix import has_prior_conflict_fix
        board = Board()
        board.active.append(Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="x",
            type="conflict-fix", review_of_assignment_id="abc123",
        ))
        assert has_prior_conflict_fix(board, "abc123") is True

    def test_true_when_completed_has_matching_conflict_fix(self) -> None:
        from coord.conflict_fix import has_prior_conflict_fix
        board = Board()
        board.completed.append(Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="x",
            type="conflict-fix", review_of_assignment_id="abc123",
        ))
        assert has_prior_conflict_fix(board, "abc123") is True

    def test_ignores_non_conflict_fix_types(self) -> None:
        from coord.conflict_fix import has_prior_conflict_fix
        board = Board()
        board.completed.append(Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="x",
            type="review", review_of_assignment_id="abc123",
        ))
        assert has_prior_conflict_fix(board, "abc123") is False

    def test_ignores_other_merge_entries(self) -> None:
        from coord.conflict_fix import has_prior_conflict_fix
        board = Board()
        board.completed.append(Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="x",
            type="conflict-fix", review_of_assignment_id="other-entry",
        ))
        assert has_prior_conflict_fix(board, "abc123") is False


# ── Reconcile hook ──────────────────────────────────────────────────────────


class TestReconcileHook:
    """Cover the conflict-fix completion path in `coord.reconcile`."""

    def _populate_queue(self, error: str = "Merge conflict") -> None:
        from coord import merge_queue as mq
        mq.save_queue([_entry(error=error)])

    def test_success_resets_entry_to_pending(self, coord_db) -> None:
        from coord import merge_queue as mq
        from coord.reconcile import _on_conflict_fix_done

        self._populate_queue()
        fix = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="fix-id", status="done",
            type="conflict-fix", review_of_assignment_id="abc123",
        )
        _on_conflict_fix_done(fix, succeeded=True)

        entry = mq.load_queue()[0]
        assert entry.state == PENDING
        assert entry.error is None

    def test_failure_marks_human_required(self, coord_db) -> None:
        from coord import merge_queue as mq
        from coord.reconcile import _on_conflict_fix_done

        self._populate_queue(error="Merge conflict")
        fix = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="fix-id", status="failed",
            type="conflict-fix", review_of_assignment_id="abc123",
        )
        _on_conflict_fix_done(fix, succeeded=False)

        entry = mq.load_queue()[0]
        assert entry.state == HUMAN_REQUIRED
        assert "Manual rebase required" in (entry.error or "")

    def test_noop_when_no_parent(self, coord_db) -> None:
        from coord.reconcile import _on_conflict_fix_done

        # Should not raise even if review_of_assignment_id is missing.
        _on_conflict_fix_done(
            Assignment(
                machine_name="m", repo_name="api", issue_number=1, issue_title="x",
                type="conflict-fix",
            ),
            succeeded=True,
        )

    def test_failure_posts_issue_comment(self, coord_db) -> None:
        """The coordinator posts a HUMAN_REQUIRED comment on failure.

        Replaces the worker's previous "post a comment on the issue"
        instruction (which contradicted the "don't use gh" rule). The
        comment is best-effort: the test asserts the post is attempted
        with the right repo/issue and a non-empty body.
        """
        from coord.reconcile import _on_conflict_fix_done

        self._populate_queue(error="Merge conflict in foo.py")
        fix = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="fix-id", status="failed",
            type="conflict-fix", review_of_assignment_id="abc123",
        )
        with patch("coord.github_ops.post_issue_comment") as post:
            _on_conflict_fix_done(fix, succeeded=False)
        post.assert_called_once()
        repo_arg, issue_arg, body_arg = post.call_args[0]
        assert repo_arg == "acme/api"
        assert issue_arg == 1
        assert "HUMAN_REQUIRED" in body_arg
        assert "fix-id" in body_arg
        assert "laptop" in body_arg

    def test_failure_swallows_github_post_errors(self, coord_db) -> None:
        """A failing gh post must not raise out of the reconcile hook."""
        from coord.reconcile import _on_conflict_fix_done

        self._populate_queue(error="Merge conflict")
        fix = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="fix-id", status="failed",
            type="conflict-fix", review_of_assignment_id="abc123",
        )
        with patch(
            "coord.github_ops.post_issue_comment",
            side_effect=RuntimeError("gh unauthenticated"),
        ):
            # Must not raise — comment posting is best-effort.
            _on_conflict_fix_done(fix, succeeded=False)

    def test_success_does_not_post_comment(self, coord_db) -> None:
        """Only failure triggers the HUMAN_REQUIRED comment."""
        from coord.reconcile import _on_conflict_fix_done

        self._populate_queue()
        fix = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="fix-id", status="done",
            type="conflict-fix", review_of_assignment_id="abc123",
        )
        with patch("coord.github_ops.post_issue_comment") as post:
            _on_conflict_fix_done(fix, succeeded=True)
        post.assert_not_called()
