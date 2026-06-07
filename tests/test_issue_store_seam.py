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
) -> None:
    """Insert a `running` assignment row so the seam has something to UPDATE."""
    from coord.models import Proposal

    proposal = Proposal(
        id=0,
        machine_name=machine,
        repo_name=repo_name,
        issue_number=issue_number,
        issue_title=issue_title,
        rationale="test",
        briefing="brief",
        type="work",
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
        _seed_running_assignment("aid-rev-1")
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
        _seed_running_assignment("cli-4")
        with patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                [
                    "report-result",
                    "--assignment", "cli-4",
                    "--status", "done",
                    "--verdict", "request-changes",
                    "--summary", "see body",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        row = state_mod.get_connection().execute(
            "SELECT review_verdict FROM assignments WHERE assignment_id=?",
            ("cli-4",),
        ).fetchone()
        assert row["review_verdict"] == "request-changes"

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

        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "fix X", "body": "do the thing"},
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
        _seed_running_assignment("backstop-3")
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
