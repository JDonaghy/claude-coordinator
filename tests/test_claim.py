"""Tests for issue claim detection (coord/claim.py)."""

from __future__ import annotations

import pytest

from coord.claim import (
    Claim,
    claim_message,
    find_work_claim,
    has_active_followup,
    has_active_work_followup,
)
from coord.models import Assignment, Board


# ── Helpers ────────────────────────────────────────────────────────────────


def _active(
    *,
    issue: int,
    repo: str = "api",
    machine: str = "laptop",
    branch: str | None = "issue-{n}-fix",
    aid: str = "abc",
    type_: str = "work",
    review_of: str | None = None,
) -> Assignment:
    return Assignment(
        machine_name=machine,
        repo_name=repo,
        issue_number=issue,
        issue_title="test",
        status="running",
        branch=(branch.replace("{n}", str(issue)) if branch else None),
        assignment_id=aid,
        type=type_,
        review_of_assignment_id=review_of,
    )


# ── find_work_claim ─────────────────────────────────────────────────────────


def test_no_claim_returns_none_when_board_empty_and_no_remote_branch() -> None:
    board = Board()
    claim = find_work_claim(
        16, "api", "acme/api", board, branch_lookup=lambda repo, n: []
    )
    assert claim is None


def test_board_claim_detected_when_active_assignment_matches() -> None:
    board = Board(active=[_active(issue=16, machine="server", aid="srv-1")])
    claim = find_work_claim(
        16, "api", "acme/api", board, branch_lookup=lambda repo, n: []
    )
    assert claim is not None
    assert claim.source == "board"
    assert claim.machine_name == "server"
    assert claim.assignment_id == "srv-1"
    assert claim.branch == "issue-16-fix"


def test_board_claim_ignores_other_repo() -> None:
    board = Board(active=[_active(issue=16, repo="other")])
    claim = find_work_claim(
        16, "api", "acme/api", board, branch_lookup=lambda repo, n: []
    )
    assert claim is None


def test_board_claim_ignores_other_issue() -> None:
    board = Board(active=[_active(issue=99)])
    claim = find_work_claim(
        16, "api", "acme/api", board, branch_lookup=lambda repo, n: []
    )
    assert claim is None


def test_remote_branch_claim_detected_when_board_clean() -> None:
    board = Board()
    claim = find_work_claim(
        16, "api", "acme/api", board,
        branch_lookup=lambda repo, n: ["issue-16-add-thing"],
    )
    assert claim is not None
    assert claim.source == "remote_branch"
    assert claim.branch == "issue-16-add-thing"


def test_board_claim_takes_priority_over_remote() -> None:
    """Board claim is cheaper to detect and more specific — return it first."""
    board = Board(active=[_active(issue=16, machine="server")])
    claim = find_work_claim(
        16, "api", "acme/api", board,
        branch_lookup=lambda repo, n: ["issue-16-something"],
    )
    assert claim is not None
    assert claim.source == "board"


def test_branch_lookup_receives_repo_github_and_issue_number() -> None:
    seen: list[tuple[str, int]] = []

    def lookup(repo, n):
        seen.append((repo, n))
        return []

    find_work_claim(42, "api", "acme/api", Board(), branch_lookup=lookup)
    assert seen == [("acme/api", 42)]


# ── claim_message ───────────────────────────────────────────────────────────


def test_claim_message_for_board_includes_machine_and_branch() -> None:
    msg = claim_message(Claim(
        issue_number=16, repo_name="api", source="board",
        machine_name="server", assignment_id="srv-1", branch="issue-16-fix",
    ))
    assert "#16" in msg
    assert "api" in msg
    assert "server" in msg
    assert "srv-1" in msg
    assert "issue-16-fix" in msg


def test_claim_message_for_board_handles_missing_fields() -> None:
    msg = claim_message(Claim(
        issue_number=16, repo_name="api", source="board",
        machine_name=None, assignment_id=None, branch=None,
    ))
    # Doesn't crash, doesn't include "by None" or empty parens
    assert "#16" in msg
    assert "None" not in msg
    assert "()" not in msg


def test_claim_message_for_remote_branch() -> None:
    msg = claim_message(Claim(
        issue_number=16, repo_name="api", source="remote_branch",
        branch="issue-16-foo",
    ))
    assert "#16" in msg
    assert "remote branch" in msg
    assert "issue-16-foo" in msg


# ── has_active_followup ─────────────────────────────────────────────────────


def test_has_active_followup_finds_in_flight_review() -> None:
    board = Board(active=[
        _active(issue=16, type_="review", review_of="work-1", aid="rev-1"),
    ])
    assert has_active_followup(
        board, of_assignment_id="work-1", assignment_type="review"
    )


def test_has_active_followup_distinguishes_type() -> None:
    """A review in flight should NOT block a smoke dispatch."""
    board = Board(active=[
        _active(issue=16, type_="review", review_of="work-1"),
    ])
    assert not has_active_followup(
        board, of_assignment_id="work-1", assignment_type="smoke"
    )


def test_has_active_followup_distinguishes_target_assignment() -> None:
    """A review of one work assignment shouldn't block reviews of another."""
    board = Board(active=[
        _active(issue=16, type_="review", review_of="work-1"),
    ])
    assert not has_active_followup(
        board, of_assignment_id="work-2", assignment_type="review"
    )


def test_has_active_followup_returns_false_for_none_target() -> None:
    """No work assignment ID → can't dedupe; allow the dispatch."""
    board = Board(active=[_active(issue=16, type_="review", review_of=None)])
    assert not has_active_followup(
        board, of_assignment_id=None, assignment_type="review"
    )


# ── Integration: dispatch_review / dispatch_smoke respect the dedupe ────────


def test_dispatch_review_skipped_when_followup_already_active() -> None:
    """When a review for the same work assignment is in flight, skip."""
    from coord.config import Config, ReviewsConfig
    from coord.models import Machine, Repo
    from coord.review import dispatch_review

    repo = Repo(name="api", github="acme/api", depends_on=[], default_branch="main")
    cfg = Config(
        repos=[repo],
        machines=[
            Machine(name="laptop", host="laptop.tail", repos=["api"],
                    repo_paths={"api": "/w"}, capabilities=[]),
            Machine(name="server", host="server.tail", repos=["api"],
                    repo_paths={"api": "/s"}, capabilities=[]),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
    )

    completed = Assignment(
        machine_name="laptop", repo_name="api", issue_number=16,
        issue_title="X", status="done", branch="issue-16-fix",
        assignment_id="work-1", type="work",
    )
    existing_review = _active(
        issue=16, type_="review", review_of="work-1", aid="rev-existing"
    )
    board = Board(active=[existing_review])

    class _Client:
        def __init__(self):
            self.calls = 0

        def post(self, url, *, json, timeout):
            self.calls += 1
            raise AssertionError("should not be called when deduped")

    client = _Client()
    result = dispatch_review(
        completed, board, cfg,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {"number": 1, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is None
    assert client.calls == 0


def test_dispatch_smoke_skipped_when_followup_already_active() -> None:
    from coord.config import Config, SmokeRule, SmokeTestsConfig
    from coord.models import Machine, Repo
    from coord.smoke import dispatch_smoke

    repo = Repo(name="api", github="acme/api", depends_on=[], default_branch="main")
    cfg = Config(
        repos=[repo],
        machines=[
            Machine(name="laptop", host="laptop.tail", repos=["api"],
                    repo_paths={"api": "/w"}, capabilities=["gtk"]),
        ],
        smoke_tests=SmokeTestsConfig(
            auto_queue=True,
            capability_rules=[SmokeRule(files=["src/"], requires=["gtk"])],
        ),
    )

    completed = Assignment(
        machine_name="laptop", repo_name="api", issue_number=16,
        issue_title="X", status="done", branch="issue-16-fix",
        assignment_id="work-1", type="work",
    )
    existing_smoke = _active(
        issue=16, type_="smoke", review_of="work-1", aid="smoke-existing"
    )
    board = Board(active=[existing_smoke])

    class _Client:
        def post(self, url, *, json, timeout):
            raise AssertionError("should not be called when deduped")

    result = dispatch_smoke(
        completed, board, cfg,
        http_client=_Client(),
        diff_lookup=lambda repo, branch: ["src/main.c"],
    )
    assert result is None


# ── Claim filtering by status and type ────────────────────────────────────


def test_failed_assignment_does_not_block_claim() -> None:
    failed = _active(issue=42, aid="old-fail")
    failed.status = "failed"
    board = Board(active=[failed])
    claim = find_work_claim(42, "api", "acme/api", board, branch_lookup=lambda *a: [])
    assert claim is None


def test_plan_assignment_does_not_block_claim() -> None:
    plan = _active(issue=42, type_="plan", aid="plan-1")
    board = Board(active=[plan])
    claim = find_work_claim(42, "api", "acme/api", board, branch_lookup=lambda *a: [])
    assert claim is None


def test_review_assignment_does_not_block_claim() -> None:
    review = _active(issue=42, type_="review", aid="rev-1", review_of="work-1")
    board = Board(active=[review])
    claim = find_work_claim(42, "api", "acme/api", board, branch_lookup=lambda *a: [])
    assert claim is None


def test_smoke_assignment_does_not_block_claim() -> None:
    smoke = _active(issue=42, type_="smoke", aid="smoke-1", review_of="work-1")
    board = Board(active=[smoke])
    claim = find_work_claim(42, "api", "acme/api", board, branch_lookup=lambda *a: [])
    assert claim is None


def test_running_work_assignment_still_blocks_claim() -> None:
    work = _active(issue=42, type_="work", aid="work-1")
    board = Board(active=[work])
    claim = find_work_claim(42, "api", "acme/api", board, branch_lookup=lambda *a: [])
    assert claim is not None
    assert claim.source == "board"


# ── has_active_work_followup (#459) ─────────────────────────────────────────


def test_has_active_work_followup_detects_running_work() -> None:
    """A running work assignment for the same issue blocks review dispatch."""
    board = Board(active=[_active(issue=16, repo="api", type_="work", aid="work-2")])
    assert has_active_work_followup(board, repo_name="api", issue_number=16)


def test_has_active_work_followup_detects_conflict_fix() -> None:
    """A running conflict-fix for the same issue also blocks review dispatch."""
    board = Board(active=[_active(issue=16, repo="api", type_="conflict-fix", aid="cf-1")])
    assert has_active_work_followup(board, repo_name="api", issue_number=16)


def test_has_active_work_followup_ignores_other_issue() -> None:
    board = Board(active=[_active(issue=99, repo="api", type_="work", aid="work-x")])
    assert not has_active_work_followup(board, repo_name="api", issue_number=16)


def test_has_active_work_followup_ignores_other_repo() -> None:
    board = Board(active=[_active(issue=16, repo="other", type_="work", aid="work-y")])
    assert not has_active_work_followup(board, repo_name="api", issue_number=16)


def test_has_active_work_followup_ignores_review_type() -> None:
    """An active review should not trigger the work-followup guard."""
    board = Board(active=[
        _active(issue=16, repo="api", type_="review", review_of="work-1", aid="rev-1"),
    ])
    assert not has_active_work_followup(board, repo_name="api", issue_number=16)


def test_has_active_work_followup_ignores_failed_work() -> None:
    """A failed work assignment is not 'active' — should not block."""
    failed = _active(issue=16, repo="api", type_="work", aid="work-bad")
    failed.status = "failed"
    board = Board(active=[failed])
    assert not has_active_work_followup(board, repo_name="api", issue_number=16)


def test_has_active_work_followup_returns_false_for_empty_board() -> None:
    assert not has_active_work_followup(Board(), repo_name="api", issue_number=16)


# ── Integration: dispatch_review respects the work-followup guard (#459) ────


def test_dispatch_review_skipped_when_active_work_rewriting_branch() -> None:
    """dispatch_review returns None when a work assignment is actively running
    for the same issue, even if the completed assignment has no review yet."""
    from coord.config import Config, ReviewsConfig
    from coord.models import Machine, Repo
    from coord.review import dispatch_review

    repo = Repo(name="api", github="acme/api", depends_on=[], default_branch="main")
    cfg = Config(
        repos=[repo],
        machines=[
            Machine(name="laptop", host="laptop.tail", repos=["api"],
                    repo_paths={"api": "/w"}, capabilities=[]),
            Machine(name="server", host="server.tail", repos=["api"],
                    repo_paths={"api": "/s"}, capabilities=[]),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
    )

    completed = Assignment(
        machine_name="laptop", repo_name="api", issue_number=16,
        issue_title="X", status="done", branch="issue-16-fix",
        assignment_id="work-1", type="work",
    )
    # A coord-bounce fix (work type) is actively rewriting the branch.
    active_fix = _active(issue=16, repo="api", type_="work", aid="work-2")
    board = Board(active=[active_fix])

    class _Client:
        def post(self, url, *, json, timeout):
            raise AssertionError("should not POST a review while fix is live")

    result = dispatch_review(
        completed, board, cfg,
        http_client=_Client(),
        pr_lookup=lambda repo_github, **kw: {"number": 1, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is None


def test_dispatch_review_proceeds_when_no_active_work() -> None:
    """dispatch_review proceeds normally when there's no active work for the issue."""
    from unittest.mock import patch
    from coord.config import Config, ReviewsConfig
    from coord.models import Machine, Repo
    from coord.review import dispatch_review

    repo = Repo(name="api", github="acme/api", depends_on=[], default_branch="main")
    cfg = Config(
        repos=[repo],
        machines=[
            Machine(name="laptop", host="laptop.tail", repos=["api"],
                    repo_paths={"api": "/w"}, capabilities=[]),
            Machine(name="server", host="server.tail", repos=["api"],
                    repo_paths={"api": "/s"}, capabilities=[]),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
    )

    completed = Assignment(
        machine_name="laptop", repo_name="api", issue_number=16,
        issue_title="X", status="done", branch="issue-16-fix",
        assignment_id="work-1", type="work",
    )
    board = Board()  # No active assignments — review should proceed.

    posted: list[dict] = []

    class _Client:
        def post(self, url, *, json, timeout):
            posted.append(json)

            class _Resp:
                def raise_for_status(self):
                    pass
                def json(self):
                    return {"assignment_id": "rev-new"}
            return _Resp()

    result = dispatch_review(
        completed, board, cfg,
        http_client=_Client(),
        pr_lookup=lambda repo_github, **kw: {"number": 1, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is not None
    assert posted, "expected an HTTP POST to dispatch the review"
