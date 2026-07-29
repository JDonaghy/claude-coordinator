"""#1534: never auto-dispatch a review for a branch with zero commits.

The incident: a ``test-author`` killed by the Claude session usage limit was
recorded ``done`` with an empty branch, and ``dispatch_pending_reviews`` fired
a metered review against it (`fad7c8b8bbd8`, elitebook, 5 min). The reviewer
diffed nothing against nothing, and — thanks to the #873 verdict drop —
returned ``review_verdict: null``, so even that produced no signal. The empty
slice looked authored *and* reviewed for two days.

The #946 enqueue-gate reasoning applies verbatim, one stage earlier: refuse to
spend a metered worker on a branch that provably has nothing on it.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from coord import github_ops
from coord.config import Config
from coord.models import Board
from coord.review import dispatch_review

from tests.test_review import (  # reuse the module's fixtures verbatim
    _FakeHTTPClient,
    _completed_assignment,
    _flood_config,
    repo,  # noqa: F401 — pytest fixture (two_machine_config depends on it)
    two_machine_config,  # noqa: F401 — pytest fixture
)

# NOTE on hermeticity: there is deliberately no autouse stub for
# ``github_ops.branch_commits_ahead``. conftest's ``_no_live_gh`` already makes
# ``github_ops._gh`` raise on an unmocked call, and the new helper catches that
# and returns ``None`` (unknown → fail open), so every pre-existing
# ``dispatch_review`` test keeps its exact previous behaviour without needing
# to know the gate exists.


def _dispatch(completed, board, cfg, *, ahead):
    return dispatch_review(
        completed, board, cfg,
        http_client=_FakeHTTPClient({"id": "review-id"}),
        pr_lookup=lambda repo_github, **kw: {
            "number": 43, "url": "https://github.com/acme/api/pull/43",
            "existed": True,
        },
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        now=123.0,
        remote_branch_checker=lambda repo, branch: True,
        commits_ahead_checker=lambda repo, base, branch: ahead,
    )


@pytest.mark.parametrize("atype", ["work", "mock-author", "test-author"])
def test_no_review_dispatched_for_zero_commit_branch(
    two_machine_config: Config, atype: str  # noqa: F811
) -> None:
    """The core #1534 assertion, for every commit-producing type."""
    board = Board()
    completed = replace(_completed_assignment(), type=atype, branch="empty-branch")

    result = _dispatch(completed, board, two_machine_config, ahead=0)

    assert result is None, "a 0-commit branch must not get a metered review"
    assert board.active == [], "no review worker may be enqueued"
    # A distinct, visible stall reason — NOT left on "pending", which would
    # make the next reconcile pass retry it forever.
    assert completed.review_state == "zero_commits"


def test_zero_commit_gate_runs_before_a_pr_is_opened(
    two_machine_config: Config,  # noqa: F811
) -> None:
    """The gate must not have the side effect it exists to prevent: opening a
    PR for an empty branch is itself GitHub noise a human has to clean up."""
    board = Board()
    completed = replace(_completed_assignment(), branch="empty-branch")
    opened: list[str] = []

    result = dispatch_review(
        completed, board, two_machine_config,
        http_client=_FakeHTTPClient({"id": "review-id"}),
        pr_lookup=lambda repo_github, **kw: opened.append(repo_github) or {
            "number": 43, "url": "u", "existed": False,
        },
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
        remote_branch_checker=lambda repo, branch: True,
        commits_ahead_checker=lambda repo, base, branch: 0,
    )

    assert result is None
    assert opened == [], "no PR may be opened for a zero-commit branch"


def test_review_dispatched_normally_when_branch_has_commits(
    two_machine_config: Config,  # noqa: F811
) -> None:
    board = Board()
    completed = replace(_completed_assignment(), branch="issue-1-fix")

    result = _dispatch(completed, board, two_machine_config, ahead=3)

    assert result is not None
    assert result.type == "review"
    assert board.active == [result]


def test_gate_fails_open_when_commit_count_is_unknown(
    two_machine_config: Config,  # noqa: F811
) -> None:
    """A ``gh`` failure yields ``None``, never ``0``. Treating "unknown" as
    "empty" would strand every real review behind a transient network blip —
    the gate exists to refuse *provably* empty branches only."""
    board = Board()
    completed = replace(_completed_assignment(), branch="issue-1-fix")

    result = _dispatch(completed, board, two_machine_config, ahead=None)

    assert result is not None
    assert completed.review_state != "zero_commits"


# ── the bulk loop's eligibility invariant ───────────────────────────────────


@pytest.mark.parametrize("status", ["failed", "advisory", "running", "cancelled"])
def test_pending_reviews_loop_only_considers_successful_completions(
    status: str,
) -> None:
    """#1534: a non-``done`` row must never be fed to ``dispatch_review``.

    A usage-limit kill is now recorded ``failed`` with ``review_state=None``,
    which used to satisfy the bulk loop's eligibility filter — ``dispatch_
    review`` then refused it internally on every single pass, and the flood
    counters counted a row that could never dispatch.
    """
    board = Board(completed=[replace(_completed_assignment(), status=status)])
    seen = _run_pending_reviews(board)
    assert seen == []


def test_pending_reviews_loop_still_dispatches_a_done_row() -> None:
    board = Board(completed=[_completed_assignment()])
    assert _run_pending_reviews(board) == ["abc123"]


def _run_pending_reviews(board: Board) -> list[str]:
    """Run the bulk loop with a recording stub, returning the ids it fed to
    ``dispatch_review``.  Uses ``_flood_config`` so the Test-before-Review gate
    is off — orthogonal to what these tests assert."""
    from coord.review import dispatch_pending_reviews

    seen: list[str] = []
    cfg = _flood_config(max_auto_dispatch_per_pass=5, flood_threshold=12)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "coord.review.dispatch_review",
            lambda c, b, cfg, **kw: seen.append(c.assignment_id) or None,
        )
        dispatch_pending_reviews(board, cfg)
    return seen


# ── the checker itself ──────────────────────────────────────────────────────


def test_branch_commits_ahead_reads_ahead_by(monkeypatch) -> None:
    monkeypatch.setattr(
        github_ops, "_gh", lambda *a, **k: '{"ahead_by": 4, "behind_by": 0}'
    )
    assert github_ops.branch_commits_ahead("acme/api", "main", "b") == 4


def test_branch_commits_ahead_reports_zero_for_an_empty_branch(monkeypatch) -> None:
    monkeypatch.setattr(
        github_ops, "_gh", lambda *a, **k: '{"ahead_by": 0, "behind_by": 12}'
    )
    assert github_ops.branch_commits_ahead("acme/api", "main", "b") == 0


@pytest.mark.parametrize(
    "payload",
    ["not json", "[]", "{}", '{"ahead_by": null}', '{"ahead_by": "3"}',
     '{"ahead_by": true}'],
)
def test_branch_commits_ahead_is_none_on_unusable_payload(
    monkeypatch, payload: str
) -> None:
    """Unknown must be ``None``, never ``0`` — ``0`` blocks a real review.

    ``true`` is called out explicitly because ``isinstance(True, int)`` is
    True in Python, so a bool would otherwise sail through as ``ahead_by == 1``
    (or, worse, ``False`` as ``0``).
    """
    monkeypatch.setattr(github_ops, "_gh", lambda *a, **k: payload)
    assert github_ops.branch_commits_ahead("acme/api", "main", "b") is None


def test_branch_commits_ahead_is_none_when_gh_raises(monkeypatch) -> None:
    def _boom(*a, **k):
        raise RuntimeError("gh: network unreachable")

    monkeypatch.setattr(github_ops, "_gh", _boom)
    assert github_ops.branch_commits_ahead("acme/api", "main", "b") is None


def test_branch_commits_ahead_short_circuits_without_a_network_call() -> None:
    """Degenerate inputs never reach ``gh``: a branch identical to its base is
    trivially 0 ahead, and a missing branch/base is unknown."""
    assert github_ops.branch_commits_ahead("acme/api", "main", "main") == 0
    assert github_ops.branch_commits_ahead("acme/api", "main", "") is None
    assert github_ops.branch_commits_ahead("acme/api", "", "b") is None
