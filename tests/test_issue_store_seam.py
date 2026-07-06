"""Tests for the #466 issue_store seam, the `coord report-result`
subcommand, and the interactive launcher git-floor backstop.

The whole point of the seam is that two mechanisms — the agent-typed
`coord report-result` and the launcher-side `finalize_interactive_exit`
backstop — fan in through a single pair of functions
(`issue_store.post_completion` / `issue_store.post_result`).  These
tests pin the resolved terminal status for each input shape so the
future #183 IssueStore refactor and the MCP server can swap in the
backend without changing the contract.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord import issue_store
from coord.cli import main
from coord import state as state_mod


# ── shared fixtures ────────────────────────────────────────────────────────


CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A working clone whose `origin` is a local bare repo.

    Returns (clone, origin).  Mirrors the #448 fixture so the
    commits-ahead primitive has something realistic to count.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main")

    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-b", "main")
    _git(clone, "config", "user.email", "t@t.com")
    _git(clone, "config", "user.name", "Test")
    _git(clone, "remote", "add", "origin", str(origin))
    (clone / "README").write_text("init\n")
    _git(clone, "add", "README")
    _git(clone, "commit", "-m", "initial")
    _git(clone, "push", "-u", "origin", "main")
    return clone, origin


def _seed_running_assignment(
    assignment_id: str,
    *,
    repo_name: str = "api",
    repo_github: str = "acme/api",
    machine: str = "laptop",
    issue_number: int = 7,
    issue_title: str = "Some work",
    assignment_type: str = "work",
) -> None:
    """Insert a `running` assignment row so the seam has something to UPDATE.

    ``assignment_type`` defaults to ``"work"``; verdict-bearing tests pass
    ``"review"`` since a review verdict may only be recorded on a review row
    (the #646 verdict-target invariant rejects a verdict on a work row).
    """
    from coord.models import Proposal

    proposal = Proposal(
        id=0,
        machine_name=machine,
        repo_name=repo_name,
        issue_number=issue_number,
        issue_title=issue_title,
        rationale="test",
        briefing="brief",
        type=assignment_type,
    )
    state_mod.record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github=repo_github,
        provider_name="claude-pty",
    )


# ── post_completion (git-floor backstop sink) ──────────────────────────────


class TestPostCompletion:
    """`post_completion` chooses DONE / ADVISORY / FAILED purely from the
    inputs the launcher learned locally — exit_code and commits_ahead."""

    def test_zero_commit_clean_exit_is_advisory(self) -> None:
        _seed_running_assignment("aid-adv-1")
        with patch("coord.github_ops.post_issue_comment") as post:
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-adv-1",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=0,
                )
            )
        assert outcome.status == "advisory"
        assert outcome.event == "advisory"
        assert outcome.posted is True
        # Local DB transitioned to advisory + review_state=advisory so the
        # reconcile review-dispatch loop will skip it.
        row = state_mod.get_connection().execute(
            "SELECT status, review_state FROM assignments WHERE assignment_id=?",
            ("aid-adv-1",),
        ).fetchone()
        assert row["status"] == "advisory"
        assert row["review_state"] == "advisory"
        # And the seam emitted a coordinator-shaped comment.
        post.assert_called_once()
        _repo, _issue, body = post.call_args.args
        assert "advisory" in body.lower()

    def test_nonzero_commit_clean_exit_is_done_and_pending_review(self) -> None:
        _seed_running_assignment("aid-done-1")
        with patch("coord.github_ops.post_issue_comment") as post:
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-done-1",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=3,
                    branch="issue-7-foo",
                )
            )
        assert outcome.status == "done"
        assert outcome.event == "completion"
        row = state_mod.get_connection().execute(
            "SELECT status, review_state, branch FROM assignments WHERE assignment_id=?",
            ("aid-done-1",),
        ).fetchone()
        assert row["status"] == "done"
        # The whole point of the seam: reconcile must dispatch review/smoke
        # identically to a claude -p worker → review_state must be "pending".
        assert row["review_state"] == "pending"
        assert row["branch"] == "issue-7-foo"
        post.assert_called_once()

    def test_unknown_commit_count_treated_as_done(self) -> None:
        """`None` from the commits-ahead primitive means git failed —
        per #448 policy we must NOT demote a clean exit to advisory."""
        _seed_running_assignment("aid-unk-1")
        with patch("coord.github_ops.post_issue_comment"):
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-unk-1",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=None,
                )
            )
        assert outcome.status == "done"

    def test_nonzero_exit_is_failed_regardless_of_commits(self) -> None:
        _seed_running_assignment("aid-fail-1")
        with patch("coord.github_ops.post_issue_comment"):
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-fail-1",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=2,
                    commits_ahead=5,
                )
            )
        assert outcome.status == "failed"

    # ── #676: chat and troubleshoot sessions are diagnostic-only ──────────────

    def test_chat_session_nonzero_exit_is_advisory_not_failed(self) -> None:
        """#676: a 'chat' session that crashes or closes non-zero must NOT leave
        a red failed box on the pipeline — it is a diagnostic, not a work unit."""
        _seed_running_assignment("aid-chat-fail", assignment_type="chat")
        with patch("coord.github_ops.post_issue_comment") as post:
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-chat-fail",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=1,          # non-zero — would be "failed" for work
                    commits_ahead=0,
                )
            )
        assert outcome.status == "advisory", (
            f"chat session crash should be advisory, got {outcome.status!r}"
        )
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("aid-chat-fail",),
        ).fetchone()
        assert row["status"] == "advisory"
        # Comment is still posted so the operator sees the session ended.
        post.assert_called_once()

    def test_troubleshoot_session_clean_exit_is_advisory_not_done(self) -> None:
        """#676: a 'troubleshoot' session with commits=None (no worktree) must not
        be marked 'done' (which would trigger review dispatch)."""
        _seed_running_assignment("aid-ts-clean", assignment_type="troubleshoot")
        with patch("coord.github_ops.post_issue_comment"):
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-ts-clean",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=None,  # no worktree — would be "done" for work
                )
            )
        assert outcome.status == "advisory", (
            f"troubleshoot session should be advisory, got {outcome.status!r}"
        )
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("aid-ts-clean",),
        ).fetchone()
        assert row["status"] == "advisory"

    def test_chat_completion_summary_describes_diagnostic_session(self) -> None:
        """#676: the advisory GitHub comment for a chat session names it as
        diagnostic-only, not a generic advisory."""
        _seed_running_assignment("aid-chat-msg", assignment_type="chat")
        with patch("coord.github_ops.post_issue_comment") as post:
            issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-chat-msg",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=0,
                )
            )
        _repo, _issue, body = post.call_args.args
        # The comment body should mention diagnostic-only or chat so the human
        # knows what closed rather than seeing a generic "0 commits" advisory.
        assert "diagnostic" in body.lower() or "chat" in body.lower(), (
            f"expected diagnostic/chat in advisory body, got: {body[:300]!r}"
        )

    # ── #812: review session that exited without capturing a verdict ────────────

    def test_review_type_without_verdict_is_failed(self) -> None:
        """#812: post_completion on a type='review' row should always produce
        'failed', not 'done'.  Reviews never commit code, so commits_ahead=None
        is the only possible value.  Reaching post_completion for a review means
        neither coord report-result nor the transcript-floor captured a verdict
        — the session was abandoned or never started.  Must NOT produce 'done'
        (which would leave the review box permanently blue/Active in the TUI)."""
        _seed_running_assignment("aid-rev-noverd", assignment_type="review")
        with patch("coord.github_ops.post_issue_comment") as post:
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-rev-noverd",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=None,  # reviews have no worktree
                )
            )
        assert outcome.status == "failed", (
            f"review without verdict should be failed, got {outcome.status!r}"
        )
        row = state_mod.get_connection().execute(
            "SELECT status, review_verdict FROM assignments WHERE assignment_id=?",
            ("aid-rev-noverd",),
        ).fetchone()
        assert row["status"] == "failed"
        assert row["review_verdict"] is None
        # A failure comment should have been posted so the operator notices.
        post.assert_called_once()
        _repo, _issue, body = post.call_args.args
        assert "failed" in body.lower() or "failure" in body.lower() or "error" in body.lower(), (
            f"expected failure marker in comment body, got: {body[:300]!r}"
        )

    def test_review_type_failed_summary_mentions_verdict(self) -> None:
        """#812: the failure comment body for a verdictless review should
        mention verdict/review so the operator understands what happened."""
        _seed_running_assignment("aid-rev-msg", assignment_type="review")
        with patch("coord.github_ops.post_issue_comment") as post:
            issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-rev-msg",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=None,
                )
            )
        _repo, _issue, body = post.call_args.args
        # The coordinator comment that wraps the summary will carry the word
        # "failure" from the format_failure wrapper — confirm the write went
        # through the failure path at all.
        assert post.called, "expected a GitHub comment to be posted"

    def test_github_post_failure_does_not_undo_state(self) -> None:
        """Comment-post failure is non-fatal — the DB write is the
        authoritative record."""
        _seed_running_assignment("aid-net-1")
        with patch(
            "coord.github_ops.post_issue_comment",
            side_effect=RuntimeError("rate limited"),
        ):
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-net-1",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=1,
                )
            )
        assert outcome.posted is False
        assert outcome.error is not None
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("aid-net-1",),
        ).fetchone()
        assert row["status"] == "done"


# ── post_result (coord report-result sink) ─────────────────────────────────


class TestPostResult:
    """`post_result` is the structured-report path the interactive agent
    invokes via `coord report-result`.  Status + verdict map onto the
    same three terminal states `post_completion` produces."""

    def test_status_done_is_pending_review(self) -> None:
        _seed_running_assignment("aid-rr-done")
        with patch("coord.github_ops.post_issue_comment") as post:
            outcome = issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-rr-done",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict=None,
                    summary="landed fix in foo.py",
                )
            )
        assert outcome.status == "done"
        row = state_mod.get_connection().execute(
            "SELECT status, review_state FROM assignments WHERE assignment_id=?",
            ("aid-rr-done",),
        ).fetchone()
        assert row["status"] == "done"
        assert row["review_state"] == "pending"
        post.assert_called_once()

    def test_status_already_implemented_is_advisory(self) -> None:
        _seed_running_assignment("aid-rr-ai")
        with patch("coord.github_ops.post_issue_comment"):
            outcome = issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-rr-ai",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="already-implemented",
                    verdict=None,
                    summary="already done in #100",
                )
            )
        assert outcome.status == "advisory"
        row = state_mod.get_connection().execute(
            "SELECT status, review_state FROM assignments WHERE assignment_id=?",
            ("aid-rr-ai",),
        ).fetchone()
        assert row["status"] == "advisory"
        assert row["review_state"] == "advisory"

    def test_status_blocked_is_failed(self) -> None:
        _seed_running_assignment("aid-rr-block")
        with patch("coord.github_ops.post_issue_comment"):
            outcome = issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-rr-block",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="blocked",
                    verdict=None,
                    summary="needs API key I don't have",
                )
            )
        assert outcome.status == "failed"
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("aid-rr-block",),
        ).fetchone()
        assert row["status"] == "failed"

    def test_verdict_persisted_on_done_review_session(self) -> None:
        """Review sessions push no commits but the agent must still produce
        a verdict.  `post_result(status=done, verdict=approve)` writes the
        verdict on the assignment row so the merge-gate sees it (mirroring
        what notify.py does for a claude -p reviewer)."""
        _seed_running_assignment("aid-rev-1", assignment_type="review")
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-rev-1",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict="approve",
                    summary="LGTM",
                )
            )
        row = state_mod.get_connection().execute(
            "SELECT review_verdict FROM assignments WHERE assignment_id=?",
            ("aid-rev-1",),
        ).fetchone()
        assert row["review_verdict"] == "approve"

    # ── #990: verdict write must not silently no-op ─────────────────────────
    #
    # These exercise `_persist_review_verdict` directly (rather than going
    # through the full `post_result` pipeline) so a blanket `get_connection`
    # failure only affects the verdict write under test, not the unrelated
    # `_update_local_state` / notification writes that happen earlier in
    # `_post_result_local`.

    @staticmethod
    def _verdict_record(assignment_id: str, verdict: str = "approve") -> "issue_store.ResultRecord":
        return issue_store.ResultRecord(
            assignment_id=assignment_id,
            machine_name="laptop",
            repo_name="api",
            repo_github="acme/api",
            issue_number=7,
            status="done",
            verdict=verdict,  # type: ignore[arg-type]
            summary="LGTM",
        )

    def test_verdict_write_retries_transient_failure_then_succeeds(self) -> None:
        """A transient failure on the FIRST attempt (simulating SQLite lock
        contention on the shared daemon DB) must be absorbed by the retry —
        the verdict still lands durably and no exception escapes."""
        import sqlite3

        _seed_running_assignment("aid-flaky", assignment_type="review")
        real_get_connection = state_mod.get_connection
        calls = {"n": 0}

        def flaky_get_connection():
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_get_connection()

        with patch("time.sleep"), \
             patch("coord.state.get_connection", side_effect=flaky_get_connection):
            issue_store._persist_review_verdict(self._verdict_record("aid-flaky"))
        assert calls["n"] >= 2, "expected at least one retry after the flaky first call"
        row = state_mod.get_connection().execute(
            "SELECT review_verdict FROM assignments WHERE assignment_id=?",
            ("aid-flaky",),
        ).fetchone()
        assert row["review_verdict"] == "approve"

    def test_verdict_write_raises_after_exhausting_retries(self) -> None:
        """#990 core regression: if the write never lands (persistent lock
        contention), the seam MUST raise instead of silently reporting
        success — the merge gate reads `review_verdict` directly, so a
        swallowed failure here would leave it silently stale while the CLI
        and the GitHub comment both claim the verdict was recorded."""
        import sqlite3

        _seed_running_assignment("aid-stuck", assignment_type="review")

        def always_locked():
            raise sqlite3.OperationalError("database is locked")

        with patch("time.sleep"), \
             patch("coord.state.get_connection", side_effect=always_locked):
            with pytest.raises(RuntimeError, match="review_verdict"):
                issue_store._persist_review_verdict(self._verdict_record("aid-stuck"))
        row = state_mod.get_connection().execute(
            "SELECT review_verdict FROM assignments WHERE assignment_id=?",
            ("aid-stuck",),
        ).fetchone()
        assert row["review_verdict"] is None, (
            "the DB must NOT show the verdict when the write never durably landed"
        )

    def test_verdict_write_raises_on_readback_mismatch(self) -> None:
        """Even when the UPDATE call itself raises nothing, a stale readback
        (the write silently no-op'd, e.g. matched zero rows) must still be
        treated as a failure — this is the "verify-after-write" half of the
        #990 fix, distinct from an exception being raised."""
        _seed_running_assignment("aid-mismatch", assignment_type="review")

        with patch("time.sleep"), patch.object(
            issue_store, "_read_review_verdict_local", return_value="request-changes",
        ):
            with pytest.raises(RuntimeError, match="readback mismatch"):
                issue_store._persist_review_verdict(self._verdict_record("aid-mismatch"))

    def test_findings_body_persisted_and_posted(self) -> None:
        """`--body-file` path: the full findings are persisted on the row (as
        the {verdict, body} JSON the fix worker's DB-cache reads) AND embedded
        in the posted comment under the `coord:review-findings` marker so a fix
        worker on any machine can recover them via the GitHub message bus."""
        from coord.comments import extract_findings_block
        from coord.state import load_assignment_review_findings

        _seed_running_assignment("aid-rev-bf", assignment_type="review")
        findings = "- src/foo.rs:10 — missing nil guard\n- src/bar.rs:5 — typo"
        with patch("coord.github_ops.post_issue_comment") as post:
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-rev-bf",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict="request-changes",
                    summary="two issues",
                    findings_body=findings,
                )
            )
        # 1. DB: full findings recoverable via the same loader the fix worker uses.
        cached = load_assignment_review_findings("aid-rev-bf")
        assert cached is not None
        verdict, body = cached
        assert verdict == "request-changes"
        assert "src/foo.rs:10" in body and "src/bar.rs:5" in body
        # 2. GitHub: the posted comment carries the parseable findings block.
        posted_body = post.call_args.args[2] if post.call_args.args[2:] else \
            post.call_args.kwargs.get("body", "")
        hit = extract_findings_block(posted_body, "aid-rev-bf")
        assert hit is not None
        assert hit[0] == "request-changes" and "src/foo.rs:10" in hit[1]

    def test_invalid_status_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid status"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="x",
                    machine_name="m",
                    repo_name="r",
                    repo_github="o/r",
                    issue_number=1,
                    status="garbage",  # type: ignore[arg-type]
                    verdict=None,
                    summary="",
                )
            )

    def test_invalid_verdict_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid verdict"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="x",
                    machine_name="m",
                    repo_name="r",
                    repo_github="o/r",
                    issue_number=1,
                    status="done",
                    verdict="please",  # type: ignore[arg-type]
                    summary="",
                )
            )

    def test_verdict_on_non_review_assignment_refused(self) -> None:
        """#646: a review verdict may only be recorded on a type="review" row.
        A `report-result --verdict` misrouted onto a WORK id must be refused —
        recording it marks the work row done and stamps a bogus review_verdict,
        which silently finalized a still-live interactive work session and hid
        the TUI reattach option. The write seam makes that state unrepresentable."""
        _seed_running_assignment("aid-work-x", assignment_type="work")
        with patch("coord.github_ops.post_issue_comment") as post:
            with pytest.raises(ValueError, match="not 'review'"):
                issue_store.post_result(
                    issue_store.ResultRecord(
                        assignment_id="aid-work-x",
                        machine_name="laptop",
                        repo_name="api",
                        repo_github="acme/api",
                        issue_number=7,
                        status="done",
                        verdict="approve",
                        summary="LGTM",
                    )
                )
        # Nothing was written: no comment posted, row stays running, no verdict.
        post.assert_not_called()
        row = state_mod.get_connection().execute(
            "SELECT status, review_verdict FROM assignments WHERE assignment_id=?",
            ("aid-work-x",),
        ).fetchone()
        assert row["status"] == "running"
        assert row["review_verdict"] is None

    # ── #676: chat / troubleshoot may not claim done or blocked ──────────────

    def test_chat_session_done_status_refused(self) -> None:
        """#676: a type=chat session must not claim 'done' — it has no committed
        work to back a success, and doing so would fake a pipeline advance."""
        _seed_running_assignment("aid-chat-done", assignment_type="chat")
        with patch("coord.github_ops.post_issue_comment") as post:
            with pytest.raises(ValueError, match="#676"):
                issue_store.post_result(
                    issue_store.ResultRecord(
                        assignment_id="aid-chat-done",
                        machine_name="laptop",
                        repo_name="api",
                        repo_github="acme/api",
                        issue_number=7,
                        status="done",
                        verdict=None,
                        summary="issue is good to go",
                    )
                )
        # Nothing was written: no comment posted, row stays running.
        post.assert_not_called()
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("aid-chat-done",),
        ).fetchone()
        assert row["status"] == "running"

    def test_troubleshoot_session_blocked_status_refused(self) -> None:
        """#676: a type=troubleshoot session must not claim 'blocked' either —
        'blocked' → failed in the pipeline and would stall work needlessly."""
        _seed_running_assignment("aid-ts-block", assignment_type="troubleshoot")
        with patch("coord.github_ops.post_issue_comment") as post:
            with pytest.raises(ValueError, match="#676"):
                issue_store.post_result(
                    issue_store.ResultRecord(
                        assignment_id="aid-ts-block",
                        machine_name="laptop",
                        repo_name="api",
                        repo_github="acme/api",
                        issue_number=7,
                        status="blocked",
                        verdict=None,
                        summary="can't reproduce",
                    )
                )
        post.assert_not_called()
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("aid-ts-block",),
        ).fetchone()
        assert row["status"] == "running"

    def test_chat_session_already_implemented_is_allowed(self) -> None:
        """#676: 'already-implemented' → advisory is the one neutral signal
        a chat session may send ('no work was needed' — not a false done)."""
        _seed_running_assignment("aid-chat-ai", assignment_type="chat")
        with patch("coord.github_ops.post_issue_comment"):
            outcome = issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-chat-ai",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="already-implemented",
                    verdict=None,
                    summary="confirmed already fixed in #100",
                )
            )
        assert outcome.status == "advisory"
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("aid-chat-ai",),
        ).fetchone()
        assert row["status"] == "advisory"

    def test_request_changes_without_body_raises_at_seam(self) -> None:
        """#617 keystone: request-changes with no findings_body is REFUSED at
        the write seam itself — not just in the `report-result` CLI (#580).

        This is what makes the #607 silent-drop unrepresentable: the
        operator-prompt relay, the transcript-floor, and any future caller all
        funnel through `post_result`, so none of them can persist a bodyless
        request-changes.  A one-line `summary` is not enough."""
        for body in (None, "", "   \n  "):
            with pytest.raises(ValueError, match="requires findings_body"):
                issue_store.post_result(
                    issue_store.ResultRecord(
                        assignment_id="aid-rc-nobody",
                        machine_name="laptop",
                        repo_name="api",
                        repo_github="acme/api",
                        issue_number=7,
                        status="done",
                        verdict="request-changes",
                        summary="one-liner is not enough",
                        findings_body=body,
                    )
                )


# ── `coord report-result` CLI ───────────────────────────────────────────────


class TestReportResultCli:
    def test_reports_done_through_seam(self, config_file: Path) -> None:
        _seed_running_assignment("cli-1")
        with patch("coord.github_ops.post_issue_comment") as post:
            result = CliRunner().invoke(
                main,
                [
                    "report-result",
                    "--assignment", "cli-1",
                    "--status", "done",
                    "--summary", "fixed it",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "status=done" in result.output
        row = state_mod.get_connection().execute(
            "SELECT status, review_state FROM assignments WHERE assignment_id=?",
            ("cli-1",),
        ).fetchone()
        assert row["status"] == "done"
        assert row["review_state"] == "pending"
        post.assert_called_once()

    def test_reports_already_implemented_as_advisory(
        self, config_file: Path,
    ) -> None:
        _seed_running_assignment("cli-2")
        with patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                [
                    "report-result",
                    "--assignment", "cli-2",
                    "--status", "already-implemented",
                    "--summary", "found in #100",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("cli-2",),
        ).fetchone()
        assert row["status"] == "advisory"

    def test_reports_blocked_as_failed(self, config_file: Path) -> None:
        _seed_running_assignment("cli-3")
        with patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                [
                    "report-result",
                    "--assignment", "cli-3",
                    "--status", "blocked",
                    "--summary", "needs human",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("cli-3",),
        ).fetchone()
        assert row["status"] == "failed"

    def test_review_verdict_recorded(self, config_file: Path) -> None:
        _seed_running_assignment("cli-4", assignment_type="review")
        with patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                [
                    "report-result",
                    "--assignment", "cli-4",
                    "--status", "done",
                    "--verdict", "request-changes",
                    "--summary", "see body",
                    # #580: request-changes requires the findings body.
                    "--body", "- foo.rs:10 missing guard",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        row = state_mod.get_connection().execute(
            "SELECT review_verdict, review_findings FROM assignments WHERE assignment_id=?",
            ("cli-4",),
        ).fetchone()
        assert row["review_verdict"] == "request-changes"
        # The body is persisted (not silently discarded).
        assert row["review_findings"] and "foo.rs:10" in row["review_findings"]

    def test_verdict_persist_failure_exits_nonzero(self, config_file: Path) -> None:
        """#990: if the verdict write can't be durably confirmed (retries
        exhausted in `_persist_review_verdict`), the CLI must exit non-zero
        and print a clear error — never print "result recorded" while the
        merge-gate-critical review_verdict column never actually landed."""
        _seed_running_assignment("cli-verdict-fail", assignment_type="review")

        with patch("coord.github_ops.post_issue_comment"), patch(
            "coord.issue_store._persist_review_verdict",
            side_effect=RuntimeError(
                "failed to durably persist review_verdict='approve' for "
                "assignment 'cli-verdict-fail' after 4 attempts (#990): boom"
            ),
        ):
            result = CliRunner().invoke(
                main,
                [
                    "report-result",
                    "--assignment", "cli-verdict-fail",
                    "--status", "done",
                    "--verdict", "approve",
                    "--summary", "LGTM",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code != 0
        assert "result recorded" not in result.output
        assert "review_verdict" in result.output

    def test_request_changes_without_body_is_rejected(self, config_file: Path) -> None:
        """#580: recording request-changes with only a one-line --summary (no
        --body/--body-file) must fail loudly — never silently drop the findings."""
        _seed_running_assignment("cli-rc-nobody")
        with patch("coord.github_ops.post_issue_comment") as post:
            result = CliRunner().invoke(
                main,
                [
                    "report-result",
                    "--assignment", "cli-rc-nobody",
                    "--status", "done",
                    "--verdict", "request-changes",
                    "--summary", "looks wrong",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 2
        assert "requires the review body" in result.output
        # Nothing recorded / posted — the operator must re-run with the body.
        post.assert_not_called()
        row = state_mod.get_connection().execute(
            "SELECT review_verdict FROM assignments WHERE assignment_id=?",
            ("cli-rc-nobody",),
        ).fetchone()
        assert row["review_verdict"] is None

    def test_approve_without_body_still_ok(self, config_file: Path) -> None:
        """approve needs no findings — there's nothing to fix."""
        _seed_running_assignment("cli-ap", assignment_type="review")
        with patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                [
                    "report-result",
                    "--assignment", "cli-ap",
                    "--status", "done",
                    "--verdict", "approve",
                    "--summary", "LGTM",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output

    def test_missing_assignment_id_errors(self, config_file: Path) -> None:
        """No --assignment and no $COORD_ASSIGNMENT_ID → user-facing error."""
        # Pass None for the var explicitly — Click's CliRunner only iterates
        # the env dict to apply overrides; an empty dict leaves os.environ
        # untouched, so a COORD_ASSIGNMENT_ID leaked from a prior test would
        # silently make this test pass with the wrong code path.
        result = CliRunner().invoke(
            main,
            [
                "report-result",
                "--status", "done",
                "--summary", "x",
                "--config", str(config_file),
            ],
            env={"COORD_ASSIGNMENT_ID": None},
        )
        assert result.exit_code == 2
        assert "assignment" in result.output.lower()

    def test_unknown_assignment_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            [
                "report-result",
                "--assignment", "no-such-id",
                "--status", "done",
                "--config", str(config_file),
            ],
        )
        assert result.exit_code == 1
        assert "could not resolve" in result.output


# ── interactive launcher: claim-at-start ───────────────────────────────────


class TestInteractiveClaim:
    def test_interactive_records_dispatched_assignment(
        self, config_file: Path,
    ) -> None:
        """The interactive launcher must INSERT an assignment row up
        front so claim-detection can refuse parallel dispatches and the
        seam has a row to UPDATE on exit."""
        from coord.interactive import InteractiveFinalizeResult

        # Mock the worktree creation so the test doesn't need a real git
        # repo at /tmp/api.  Return a fake (Path, branch_name) tuple that
        # the CLI converts to a string cwd for the launcher.
        # Patch gethostname so the 'laptop' machine is detected as local
        # regardless of what machine the tests run on (#494 added
        # local/remote detection keyed off the hostname).
        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "fix X", "body": "do the thing"},
        ), patch(
            "socket.gethostname",
            return_value="laptop",
        ), patch(
            "coord.agent.setup_interactive_worktree",
            return_value=(Path("/tmp/mock-wt-42"), "issue-42-fix-x"),
        ), patch(
            "coord.interactive.launch_human_attended_interactive",
            return_value=0,
        ) as launch, patch(
            "coord.interactive.finalize_interactive_exit",
            return_value=InteractiveFinalizeResult(
                terminal_status="done",
                commits_ahead=2,
                push_ok=True,
                push_error=None,
                already_recorded=False,
                seam_outcome=None,
            ),
        ) as finalize:
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "42",
                    "--config", str(config_file),
                    "--interactive",
                ],
            )
        # SystemExit(0) → click maps to exit_code=0.
        assert result.exit_code == 0, result.output
        launch.assert_called_once()
        finalize.assert_called_once()

        records = state_mod.load_dispatched()
        assert len(records) == 1
        rec = records[0]
        assert rec["machine_name"] == "laptop"
        assert rec["repo_name"] == "api"
        assert rec["issue_number"] == 42
        # The assignment id must be injected into the process env so the
        # interactive agent can run `coord report-result --assignment
        # $COORD_ASSIGNMENT_ID`.  Wrap the assertion + cleanup in
        # try/finally so the env var is always removed even when the
        # assertion fails — a leaked var would contaminate later tests.
        import os
        try:
            assert os.environ.get("COORD_ASSIGNMENT_ID"), (
                "COORD_ASSIGNMENT_ID was not set in the process env"
            )
        finally:
            os.environ.pop("COORD_ASSIGNMENT_ID", None)

    def test_interactive_refuses_duplicate_via_claim_check(
        self, config_file: Path,
    ) -> None:
        from coord.claim import Claim

        fake_claim = Claim(
            issue_number=42, repo_name="api", source="board",
            machine_name="server", assignment_id="prior-1",
        )
        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "fix", "body": ""},
        ), patch(
            "coord.claim.find_work_claim",
            return_value=fake_claim,
        ), patch(
            "coord.claim.claim_message",
            return_value="already assigned to server",
        ), patch(
            "coord.interactive.launch_human_attended_interactive",
        ) as launch:
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "42",
                    "--config", str(config_file),
                    "--interactive",
                ],
            )
        assert result.exit_code == 1
        assert "skipping" in result.output.lower()
        launch.assert_not_called()

    def test_interactive_dry_run_does_not_record_assignment(
        self, config_file: Path,
    ) -> None:
        """`--interactive --dry-run` must NOT write a phantom `running`
        row to the DB, set ``COORD_ASSIGNMENT_ID``, or call the
        launcher.  Otherwise the user's standard "dry-run then real"
        workflow leaves a stuck row that claim-detection then refuses
        the real invocation against."""
        import os

        # Make sure the env var is clean before the test so we can
        # confidently assert it's still unset afterwards.
        had_env = "COORD_ASSIGNMENT_ID" in os.environ
        prior_env = os.environ.get("COORD_ASSIGNMENT_ID")
        os.environ.pop("COORD_ASSIGNMENT_ID", None)

        try:
            with patch(
                "coord.github_ops.get_issue",
                return_value={"title": "fix X", "body": "do the thing"},
            ), patch(
                "coord.interactive.launch_human_attended_interactive",
            ) as launch, patch(
                "coord.interactive.finalize_interactive_exit",
            ) as finalize:
                result = CliRunner().invoke(
                    main,
                    [
                        "assign", "laptop", "api", "42",
                        "--config", str(config_file),
                        "--interactive",
                        "--dry-run",
                    ],
                )

            assert result.exit_code == 0, result.output
            assert "dry run" in result.output.lower()
            # Nothing should be launched or finalized in dry-run mode.
            launch.assert_not_called()
            finalize.assert_not_called()
            # No assignment row should be written — the next real
            # invocation against the same issue would otherwise be
            # refused by claim-detection.
            records = state_mod.load_dispatched()
            assert len(records) == 0, (
                "dry-run wrote a phantom assignment row: "
                f"{[r.get('assignment_id') for r in records]}"
            )
            # The dispatch-time env var must not leak from dry-run.
            assert "COORD_ASSIGNMENT_ID" not in os.environ
        finally:
            # Restore whatever the env looked like before the test.
            os.environ.pop("COORD_ASSIGNMENT_ID", None)
            if had_env and prior_env is not None:
                os.environ["COORD_ASSIGNMENT_ID"] = prior_env


# ── git-floor backstop: real git operations ────────────────────────────────


class TestFinalizeBackstop:
    """The launcher-side `finalize_interactive_exit` is the git-floor
    backstop: counts commits, pushes, ALWAYS writes a terminal state."""

    def test_backstop_done_with_commits(
        self, repo_with_remote: tuple[Path, Path],
    ) -> None:
        from coord.interactive import finalize_interactive_exit

        clone, _origin = repo_with_remote
        _git(clone, "checkout", "-b", "issue-7-x")
        (clone / "fix.py").write_text("# fix\n")
        _git(clone, "add", "fix.py")
        _git(clone, "commit", "-m", "real work")

        _seed_running_assignment("backstop-1")
        with patch("coord.github_ops.post_issue_comment"):
            result = finalize_interactive_exit(
                assignment_id="backstop-1",
                repo_name="api",
                repo_github="acme/api",
                issue_number=7,
                machine_name="laptop",
                worktree_path=str(clone),
                base_branch="main",
                exit_code=0,
                started_at=None,
            )
        assert result.already_recorded is False
        assert result.terminal_status == "done"
        assert result.commits_ahead == 1
        assert result.push_ok is True
        # Branch was captured.
        row = state_mod.get_connection().execute(
            "SELECT status, review_state, branch FROM assignments WHERE assignment_id=?",
            ("backstop-1",),
        ).fetchone()
        assert row["status"] == "done"
        assert row["review_state"] == "pending"
        assert row["branch"] == "issue-7-x"

    def test_backstop_advisory_with_zero_commits(
        self, repo_with_remote: tuple[Path, Path],
    ) -> None:
        from coord.interactive import finalize_interactive_exit

        clone, _origin = repo_with_remote
        # Stay on main with no new commits.
        _seed_running_assignment("backstop-2")
        with patch("coord.github_ops.post_issue_comment"):
            result = finalize_interactive_exit(
                assignment_id="backstop-2",
                repo_name="api",
                repo_github="acme/api",
                issue_number=8,
                machine_name="laptop",
                worktree_path=str(clone),
                base_branch="main",
                exit_code=0,
                started_at=None,
            )
        assert result.terminal_status == "advisory"
        assert result.commits_ahead == 0
        row = state_mod.get_connection().execute(
            "SELECT status, review_state FROM assignments WHERE assignment_id=?",
            ("backstop-2",),
        ).fetchone()
        assert row["status"] == "advisory"
        assert row["review_state"] == "advisory"

    def test_backstop_respects_prior_report_result(
        self, repo_with_remote: tuple[Path, Path],
    ) -> None:
        """If `coord report-result` already wrote a terminal state, the
        backstop must NOT clobber it.  Review sessions (which legitimately
        have 0 commits) would otherwise lose their agent-typed verdict."""
        from coord.interactive import finalize_interactive_exit

        clone, _origin = repo_with_remote
        _seed_running_assignment("backstop-3", assignment_type="review")
        # Simulate `coord report-result` having already written DONE.
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="backstop-3",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=9,
                    status="done",
                    verdict="approve",
                    summary="reviewed",
                )
            )

        with patch("coord.github_ops.post_issue_comment") as post:
            result = finalize_interactive_exit(
                assignment_id="backstop-3",
                repo_name="api",
                repo_github="acme/api",
                issue_number=9,
                machine_name="laptop",
                worktree_path=str(clone),
                base_branch="main",
                exit_code=0,
                started_at=None,
            )
        assert result.already_recorded is True
        # No GitHub re-post; the agent's report wins.
        post.assert_not_called()
        # And the prior DONE / approve verdict is still there.
        row = state_mod.get_connection().execute(
            "SELECT status, review_verdict FROM assignments WHERE assignment_id=?",
            ("backstop-3",),
        ).fetchone()
        assert row["status"] == "done"
        assert row["review_verdict"] == "approve"

    def test_backstop_failed_on_nonzero_exit(
        self, repo_with_remote: tuple[Path, Path],
    ) -> None:
        """Non-zero exit → failed regardless of commit count."""
        from coord.interactive import finalize_interactive_exit

        clone, _origin = repo_with_remote
        _seed_running_assignment("backstop-4")
        with patch("coord.github_ops.post_issue_comment"):
            result = finalize_interactive_exit(
                assignment_id="backstop-4",
                repo_name="api",
                repo_github="acme/api",
                issue_number=10,
                machine_name="laptop",
                worktree_path=str(clone),
                base_branch="main",
                exit_code=130,  # ctrl-c
                started_at=None,
            )
        assert result.terminal_status == "failed"
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("backstop-4",),
        ).fetchone()
        assert row["status"] == "failed"


# ── reconcile parity: interactive completions dispatch review/smoke ────────


class TestReconcileParity:
    """The seam writes interactive completions into the same shape a
    claude -p completion has (status=done, review_state=pending,
    branch set).  reconcile()'s review-dispatch loop must therefore
    pick them up identically to a remote-agent worker completion."""

    def test_done_interactive_is_eligible_for_review_dispatch(
        self,
    ) -> None:
        """build_board()→reconcile() iterates board.completed for
        review dispatch.  An interactive `done` row must show up as a
        completed work assignment with `review_state='pending'` and a
        branch — exactly the same shape `reconcile` looks for."""
        _seed_running_assignment("rp-1")
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="rp-1",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=11,
                    exit_code=0,
                    commits_ahead=2,
                    branch="issue-11-feat",
                )
            )
        board = state_mod.build_board()
        # Live row is in completed, not active.
        assert all(a.assignment_id != "rp-1" for a in board.active)
        match = [a for a in board.completed if a.assignment_id == "rp-1"]
        assert len(match) == 1
        done = match[0]
        # Same fields reconcile.dispatch_review consumes:
        assert done.status == "done"
        assert done.type == "work"
        assert done.review_state == "pending"
        assert done.branch == "issue-11-feat"

    def test_advisory_interactive_is_skipped_by_review_dispatch(self) -> None:
        """Reconcile's review loop filters `review_state not in (None,
        "pending")`, so an interactive advisory (review_state=advisory)
        must not be picked up.  Same shape as the #448 advisory state."""
        _seed_running_assignment("rp-2")
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="rp-2",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=12,
                    exit_code=0,
                    commits_ahead=0,
                )
            )
        board = state_mod.build_board()
        match = [a for a in board.completed if a.assignment_id == "rp-2"]
        assert len(match) == 1
        assert match[0].review_state == "advisory"
