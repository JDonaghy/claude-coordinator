"""Tests for coord.overlap_fence — #1720 dispatch-time file-overlap fence."""

from __future__ import annotations

from coord.models import Assignment, Board
from coord.overlap_fence import compute_overlap_fence


def _running(
    issue_number: int, branch: str | None, *, repo_name: str = "api", type_: str = "work",
) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name=repo_name,
        issue_number=issue_number,
        issue_title=f"issue {issue_number}",
        status="running",
        type=type_,
        branch=branch,
    )


class TestComputeOverlapFence:
    def test_names_issue_and_files_on_overlap(self) -> None:
        board = Board(active=[_running(10, "fix-10")])
        fetcher = lambda repo, base, head: ["src/foo.py", "src/bar.py"]  # noqa: E731

        fence = compute_overlap_fence(
            "api", "acme/api", "main",
            exclude_issue_number=20,
            board=board,
            diff_files_fetcher=fetcher,
        )

        assert "#10" in fence
        assert "fix-10" in fence
        assert "src/foo.py" in fence
        assert "src/bar.py" in fence

    def test_no_running_assignments_is_empty(self) -> None:
        board = Board(active=[])
        fence = compute_overlap_fence(
            "api", "acme/api", "main",
            exclude_issue_number=20,
            board=board,
            diff_files_fetcher=lambda repo, base, head: ["should-not-be-called.py"],
        )
        assert fence == ""

    def test_no_running_work_like_assignments_is_empty(self) -> None:
        # Only non-work-like types (e.g. review/smoke) running — not a fence
        # candidate; brain.py's guess-from-body heuristic is for `coord plan`
        # only, this fence is about *dispatched, running* work.
        board = Board(active=[_running(10, "fix-10", type_="review")])
        fence = compute_overlap_fence(
            "api", "acme/api", "main",
            exclude_issue_number=20,
            board=board,
            diff_files_fetcher=lambda repo, base, head: ["src/foo.py"],
        )
        assert fence == ""

    def test_running_assignment_with_no_branch_contributes_nothing(self) -> None:
        # No pushed commits yet -> no branch on the row -> excluded before
        # any diff fetch is even attempted (never guesses from the issue body).
        called = []
        board = Board(active=[_running(10, None)])

        def fetcher(repo, base, head):
            called.append((repo, base, head))
            return ["should-not-happen.py"]

        fence = compute_overlap_fence(
            "api", "acme/api", "main",
            exclude_issue_number=20,
            board=board,
            diff_files_fetcher=fetcher,
        )
        assert fence == ""
        assert called == []

    def test_unreadable_branch_is_skipped_not_fatal(self) -> None:
        # One assignment's diff fails (deleted/unreachable branch); another
        # succeeds. The failure must not sink the whole fence.
        board = Board(active=[_running(10, "gone-branch"), _running(11, "ok-branch")])

        def fetcher(repo, base, head):
            if head == "gone-branch":
                raise RuntimeError("gh: branch not found")
            return ["src/ok.py"]

        fence = compute_overlap_fence(
            "api", "acme/api", "main",
            exclude_issue_number=20,
            board=board,
            diff_files_fetcher=fetcher,
        )
        assert "#11" in fence
        assert "src/ok.py" in fence
        assert "#10" not in fence

    def test_fetcher_returning_none_is_skipped(self) -> None:
        # get_compare_files' own documented failure contract: None, not [].
        board = Board(active=[_running(10, "fix-10")])
        fence = compute_overlap_fence(
            "api", "acme/api", "main",
            exclude_issue_number=20,
            board=board,
            diff_files_fetcher=lambda repo, base, head: None,
        )
        assert fence == ""

    def test_excludes_the_issue_being_dispatched(self) -> None:
        # A redispatch of the SAME issue must not fence against itself.
        board = Board(active=[_running(20, "fix-20")])
        fence = compute_overlap_fence(
            "api", "acme/api", "main",
            exclude_issue_number=20,
            board=board,
            diff_files_fetcher=lambda repo, base, head: ["src/foo.py"],
        )
        assert fence == ""

    def test_scoped_to_the_target_repo(self) -> None:
        board = Board(active=[_running(10, "fix-10", repo_name="other-repo")])
        fence = compute_overlap_fence(
            "api", "acme/api", "main",
            exclude_issue_number=20,
            board=board,
            diff_files_fetcher=lambda repo, base, head: ["src/foo.py"],
        )
        assert fence == ""

    def test_board_read_failure_fails_open(self) -> None:
        # No injected board -> falls back to reading the live board; patch
        # that seam to explode and confirm the fence degrades to "" rather
        # than propagating the exception (an unreachable daemon must never
        # block a dispatch).
        import coord.overlap_fence as overlap_fence_mod

        original = overlap_fence_mod.compute_overlap_fence

        def boom():
            raise RuntimeError("board unreachable")

        # Patch the module read_board is imported from, at call time, via
        # monkeypatching the local import target.
        import coord.board_service as board_service_mod

        saved = board_service_mod.read_board
        board_service_mod.read_board = boom
        try:
            fence = original("api", "acme/api", "main", exclude_issue_number=20)
        finally:
            board_service_mod.read_board = saved
        assert fence == ""
