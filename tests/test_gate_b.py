"""Unit tests for Gate B (#933, docs/PIPELINE_V2.md) — the post-milestone
architecture review: `coord/gate_b.py`'s pure briefing builder and its
`dispatch_gate_b_review` dispatch helper.

Mirrors the DI style used throughout milestone_dispatch.py / review.py: no
real network or `gh` calls, everything injected.
"""

from __future__ import annotations

from unittest.mock import patch

from coord.gate_b import (
    build_gate_b_briefing,
    dispatch_gate_b_review,
    latest_gate_b_verdict,
    review_target_for,
)
from coord.milestone_order import WorkOrder, WorkOrderNode
from coord.models import Assignment, Board, Machine, Repo
from coord.review import REVIEWER_SYSTEM_PROMPT


class _FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeHTTPClient:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, *, json: dict, timeout: float) -> _FakeHTTPResponse:
        self.calls.append((url, json))
        return _FakeHTTPResponse(self._payload)


class _RejectingHTTPClient:
    def post(self, url: str, *, json: dict, timeout: float):
        import httpx

        raise httpx.ConnectError("connection refused")


def _repo() -> Repo:
    return Repo(name="coord-tui", github="acme/coord-tui", default_branch="main")


def _machine() -> Machine:
    return Machine(
        name="laptop", host="laptop.tail", repos=["coord-tui"],
        repo_paths={"coord-tui": "/home/x/coord-tui"},
    )


def _work_order() -> WorkOrder:
    return WorkOrder(nodes=(
        WorkOrderNode(issue_number=930),
        WorkOrderNode(issue_number=931),
    ))


class FakeConfig:
    class _Models:
        default = "sonnet"

    models = _Models()


def _issue_fetch(repo_github: str, issue_number: int) -> dict:
    return {"number": issue_number, "title": f"issue {issue_number}"}


def _contract_fetch_with_text(repo_github: str, path: str, branch: str) -> str | None:
    return "# Contract\n\n- CLI: `coord frobnicate`\n"


def _contract_fetch_missing(repo_github: str, path: str, branch: str) -> str | None:
    return None


class TestReviewTargetFor:
    def test_non_numeric_sentinel(self) -> None:
        # #933: must never parse as an int — this is what makes
        # coord.notify._try_parse_and_post_review fall back to posting a
        # plain issue comment (no PR exists for a milestone review) with
        # zero changes to notify.py.
        target = review_target_for(42)
        assert "42" in target
        try:
            int(target)
            raised = False
        except ValueError:
            raised = True
        assert raised, f"review_target_for() must not be int-parseable, got {target!r}"


class TestBuildGateBBriefing:
    def _build(self, contract_text: str | None) -> str:
        return build_gate_b_briefing(
            tracking_issue=929,
            milestone_number=17,
            tracking_issue_title="Epic: two-tier milestone pipeline",
            tracking_issue_body="## Work order\n- [ ] #930\n- [ ] #931\n",
            member_issues=[(930, "Gate A"), (931, "acceptance authoring")],
            contract_text=contract_text,
            repo_github="acme/coord-tui",
            default_branch="main",
        )

    def test_includes_tracking_issue_and_member_issues(self) -> None:
        briefing = self._build("# Contract\n- CLI: `coord frobnicate`\n")
        assert "#929" in briefing
        assert "Epic: two-tier milestone pipeline" in briefing
        assert "#930: Gate A" in briefing
        assert "#931: acceptance authoring" in briefing

    def test_embeds_contract_text_as_the_rubric(self) -> None:
        briefing = self._build("# Contract\n- CLI: `coord frobnicate`\n")
        assert "Gate-A contract" in briefing
        assert "coord frobnicate" in briefing

    def test_missing_contract_falls_back_to_tracking_issue_body(self) -> None:
        briefing = self._build(None)
        assert "No Gate-A contract found" in briefing
        assert "tracking issue" in briefing.lower()

    def test_output_contract_matches_existing_review_verdict_parser(self) -> None:
        # Reuses coord.review's exact REVIEW_VERDICT/REVIEW_BODY/END_REVIEW
        # protocol so coord.notify's parsing needs zero changes for Gate B.
        briefing = self._build("contract")
        assert "REVIEW_VERDICT: approve" in briefing
        assert "REVIEW_BODY:" in briefing
        assert "END_REVIEW" in briefing
        assert "request-changes" in briefing

    def test_instructs_reviewer_not_to_touch_docs(self) -> None:
        # #933 scope guard: "Do not touch README/CHANGELOG/docs."
        briefing = self._build("contract")
        assert "Do NOT touch README/CHANGELOG" in briefing

    def test_default_branch_appears_in_checkout_instructions(self) -> None:
        briefing = self._build("contract")
        assert "origin/main" in briefing


class TestDispatchGateBReview:
    def test_happy_path_posts_review_type_assignment(self) -> None:
        board = Board()
        client = _FakeHTTPClient({"id": "gb-1"})
        with patch("coord.github_ops.post_issue_comment") as mock_comment:
            assignment = dispatch_gate_b_review(
                repo_cfg=_repo(),
                config=FakeConfig(),
                machine=_machine(),
                tracking_issue=929,
                milestone_number=17,
                work_order=_work_order(),
                board=board,
                http_client=client,
                contract_fetch=_contract_fetch_with_text,
                issue_fetch=_issue_fetch,
                now=1000.0,
            )

        assert assignment.type == "review"
        assert assignment.issue_number == 929
        assert assignment.review_target == "gate-b-ms-17"
        # Never int-parseable — the whole point of the sentinel.
        assert assignment.review_of_assignment_id is None
        assert assignment.assignment_id == "gb-1"
        assert assignment in board.active

        # The dispatched payload used the reviewer's no-gh system prompt and
        # targeted the milestone's default branch, not a PR/worker branch.
        assert len(client.calls) == 1
        _url, payload = client.calls[0]
        assert payload["type"] == "review"
        assert payload["system_prompt"] == REVIEWER_SYSTEM_PROMPT
        assert payload["branch"] == "main"
        assert payload["issue_number"] == 929
        assert "930" in payload["briefing"]
        assert "931" in payload["briefing"]

        # The tracking issue gets an announcement comment (no gh call from
        # the reviewer itself — the coordinator posts it, same posture as
        # a per-issue review's briefing comment).
        mock_comment.assert_called_once()
        posted_repo, posted_issue, _body = mock_comment.call_args[0]
        assert posted_repo == "acme/coord-tui"
        assert posted_issue == 929

    def test_raises_gate_b_error_when_machine_unreachable(self) -> None:
        from coord.gate_b import GateBError

        board = Board()
        try:
            dispatch_gate_b_review(
                repo_cfg=_repo(),
                config=FakeConfig(),
                machine=_machine(),
                tracking_issue=929,
                milestone_number=17,
                work_order=_work_order(),
                board=board,
                http_client=_RejectingHTTPClient(),
                contract_fetch=_contract_fetch_with_text,
                issue_fetch=_issue_fetch,
            )
            raised = False
        except GateBError:
            raised = True
        assert raised
        # A failed dispatch must not leave a phantom running assignment.
        assert board.active == []

    def test_raises_gate_b_error_when_machine_missing_repo_path(self) -> None:
        from coord.gate_b import GateBError

        board = Board()
        bare_machine = Machine(name="bare", host="bare.tail", repos=["coord-tui"])
        try:
            dispatch_gate_b_review(
                repo_cfg=_repo(),
                config=FakeConfig(),
                machine=bare_machine,
                tracking_issue=929,
                milestone_number=17,
                work_order=_work_order(),
                board=board,
                http_client=_FakeHTTPClient({"id": "gb-1"}),
                contract_fetch=_contract_fetch_with_text,
                issue_fetch=_issue_fetch,
            )
            raised = False
        except GateBError:
            raised = True
        assert raised

    def test_missing_contract_still_dispatches(self) -> None:
        board = Board()
        with patch("coord.github_ops.post_issue_comment"):
            assignment = dispatch_gate_b_review(
                repo_cfg=_repo(),
                config=FakeConfig(),
                machine=_machine(),
                tracking_issue=929,
                milestone_number=17,
                work_order=_work_order(),
                board=board,
                http_client=_FakeHTTPClient({"id": "gb-1"}),
                contract_fetch=_contract_fetch_missing,
                issue_fetch=_issue_fetch,
            )
        assert "No Gate-A contract found" in assignment.briefing


def _gate_b_review(
    *, verdict: str | None, dispatched_at: float = 0.0, status: str = "done",
) -> Assignment:
    return Assignment(
        machine_name="m1", repo_name="coord-tui", issue_number=929,
        issue_title="[gate-b] tracking", assignment_id=f"gb-{dispatched_at}",
        type="review", status=status, review_target=review_target_for(17),
        review_verdict=verdict, dispatched_at=dispatched_at,
    )


class TestLatestGateBVerdict:
    """#934: `coord milestone ship` (Gate D) consults this to refuse
    shipping until Gate B is green."""

    def test_no_review_found_returns_none(self) -> None:
        board = Board()
        assert latest_gate_b_verdict(board, "coord-tui", 929, 17) is None

    def test_finds_approved_verdict_on_completed(self) -> None:
        board = Board(completed=[_gate_b_review(verdict="approve")])
        assert latest_gate_b_verdict(board, "coord-tui", 929, 17) == "approve"

    def test_finds_request_changes_verdict(self) -> None:
        board = Board(completed=[_gate_b_review(verdict="request-changes")])
        assert latest_gate_b_verdict(board, "coord-tui", 929, 17) == "request-changes"

    def test_also_scans_active_board(self) -> None:
        """A verdict just posted may still be on `active` for a tick before
        reconcile moves it to `completed`."""
        board = Board(active=[_gate_b_review(verdict="approve")])
        assert latest_gate_b_verdict(board, "coord-tui", 929, 17) == "approve"

    def test_ignores_unrelated_reviews(self) -> None:
        """A per-issue review (different review_target) must not be mistaken
        for the Gate B review."""
        unrelated = Assignment(
            machine_name="m1", repo_name="coord-tui", issue_number=929,
            issue_title="t", assignment_id="rev-1", type="review",
            status="done", review_target="42", review_verdict="approve",
        )
        board = Board(completed=[unrelated])
        assert latest_gate_b_verdict(board, "coord-tui", 929, 17) is None

    def test_ignores_gate_b_for_a_different_milestone(self) -> None:
        other_ms = _gate_b_review(verdict="approve")
        other_ms.review_target = review_target_for(18)
        board = Board(completed=[other_ms])
        assert latest_gate_b_verdict(board, "coord-tui", 929, 17) is None

    def test_most_recent_wins_after_redispatch(self) -> None:
        """A request-changes verdict followed by a re-dispatched, approved
        Gate B — the LATER one (by dispatched_at) must win."""
        older = _gate_b_review(verdict="request-changes", dispatched_at=1.0)
        newer = _gate_b_review(verdict="approve", dispatched_at=2.0)
        board = Board(completed=[older, newer])
        assert latest_gate_b_verdict(board, "coord-tui", 929, 17) == "approve"
