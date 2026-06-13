"""CLI tests for `coord assign`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord import state as state_mod
from coord import merge_queue as mq
from coord.cli import main


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
  - name: server
    host: server.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
"""

# Config with pipeline.labels defined to test label→gate resolution.
CONFIG_YAML_WITH_PIPELINE = """\
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
pipeline:
  default_gates: [review, merge]
  labels:
    documentation: [merge]
    hotfix: [merge]
    needs-smoke: [review, smoke, merge]
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db):
    """Provide an isolated in-memory DB for state and return a temp dir for logs."""
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestAssignValidation:
    """Test argument validation before any network calls."""

    def test_unknown_machine(self, config_file: Path, coord_dir: Path) -> None:
        result = CliRunner().invoke(
            main, ["assign", "ghost", "api", "42", "--config", str(config_file)]
        )
        assert result.exit_code == 2
        assert "ghost" in result.output

    def test_unknown_repo(self, config_file: Path, coord_dir: Path) -> None:
        result = CliRunner().invoke(
            main, ["assign", "laptop", "nope", "42", "--config", str(config_file)]
        )
        assert result.exit_code == 2
        assert "nope" in result.output

    def test_machine_cannot_work_on_repo(self, tmp_path: Path, coord_dir: Path) -> None:
        """Machine exists but doesn't list the requested repo."""
        cfg = """\
repos:
  - name: api
    github: acme/api
  - name: web
    github: acme/web
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
"""
        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(cfg)
        result = CliRunner().invoke(
            main, ["assign", "laptop", "web", "1", "--config", str(config_file)]
        )
        assert result.exit_code == 2
        assert "does not list repo" in result.output


class TestAssignDryRun:
    def test_dry_run_does_not_dispatch(self, config_file: Path, coord_dir: Path) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "Add feature X"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "42", "--config", str(config_file), "--dry-run"],
            )
        assert result.exit_code == 0
        assert "dry run" in result.output
        assert "laptop" in result.output
        assert "#42" in result.output
        assert "Add feature X" in result.output

    def test_dry_run_no_network_dispatch(self, config_file: Path, coord_dir: Path) -> None:
        """Dry run should not call dispatch or post_briefing."""
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}) as gi, \
             patch("coord.dispatch.dispatch") as disp, \
             patch("coord.dispatch.post_briefing") as brief:
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file), "--dry-run"],
            )
        assert result.exit_code == 0
        gi.assert_called_once()
        disp.assert_not_called()
        brief.assert_not_called()


class TestAssignDispatch:
    def test_successful_dispatch(self, config_file: Path, coord_dir: Path) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "Fix bug"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "abc-123"}) as disp, \
             patch("coord.github_ops.post_issue_comment") as post_comment, \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(config_file)],
            )
        assert result.exit_code == 0
        assert "dispatched" in result.output
        assert "abc-123" in result.output

        # Verify dispatch was called with a Proposal
        disp.assert_called_once()
        proposal = disp.call_args[0][0]
        assert proposal.machine_name == "laptop"
        assert proposal.repo_name == "api"
        assert proposal.issue_number == 7
        assert proposal.issue_title == "Fix bug"

    def test_dispatched_is_recorded(self, config_file: Path, coord_dir: Path) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "Fix bug"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "rec-1"}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(config_file)],
            )
        assert result.exit_code == 0

        records = state_mod.load_dispatched()
        assert len(records) == 1
        assert records[0]["assignment_id"] == "rec-1"
        assert records[0]["machine_name"] == "laptop"
        assert records[0]["repo_name"] == "api"
        assert records[0]["issue_number"] == 7

    def test_briefing_text_passed_through(self, config_file: Path, coord_dir: Path) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "b-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "5",
                    "--config", str(config_file),
                    "--briefing", "Focus on the auth module only",
                ],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.briefing == "Focus on the auth module only"

    def test_claim_check_blocks_duplicate(self, config_file: Path, coord_dir: Path) -> None:
        """If issue is already claimed, assign should refuse."""
        from coord.claim import Claim

        fake_claim = Claim(
            issue_number=7, repo_name="api", source="board",
            machine_name="server", assignment_id="old-1",
        )
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=fake_claim), \
             patch("coord.claim.claim_message", return_value="already assigned to server"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "skipping" in result.output

    def test_force_bypasses_claim_check_and_sets_fresh_branch(self, config_file: Path, coord_dir: Path) -> None:
        """--force should skip claim detection and pass fresh_branch=True to dispatch."""
        with patch("coord.github_ops.get_issue", return_value={"title": "Fix bug"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "f-1"}) as mock_dispatch, \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(config_file), "--force"],
            )
        assert result.exit_code == 0
        assert "dispatched" in result.output
        _, kwargs = mock_dispatch.call_args
        assert kwargs.get("fresh_branch") is True


    def test_dispatch_http_error(self, config_file: Path, coord_dir: Path) -> None:
        import httpx

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch(
                 "coord.dispatch.dispatch",
                 side_effect=httpx.ConnectError("connection refused"),
             ):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "dispatch failed" in result.output

    def test_issue_fetch_failure(self, config_file: Path, coord_dir: Path) -> None:
        with patch(
            "coord.github_ops.get_issue",
            side_effect=RuntimeError("gh issue view failed: not found"),
        ):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "999", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "could not fetch issue" in result.output

    def test_briefing_post_failure_is_nonfatal(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """If briefing post fails, the assignment should still succeed."""
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "ok-1"}), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch(
                 "coord.github_ops.post_issue_comment",
                 side_effect=RuntimeError("rate limited"),
             ):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "3", "--config", str(config_file)],
            )
        assert result.exit_code == 0
        assert "dispatched" in result.output
        assert "briefing post failed" in result.output


class TestAssignLabelGateResolution:
    """Tests for label→required_gates resolution in coord assign (cli.py:1200-1207)."""

    @pytest.fixture
    def pipeline_config_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(CONFIG_YAML_WITH_PIPELINE)
        return p

    def test_documentation_label_resolves_to_merge_only(
        self, pipeline_config_file: Path, coord_dir: Path
    ) -> None:
        """Issue with 'documentation' label → required_gates=["merge"] (skip review)."""
        issue_payload = {
            "title": "Update docs",
            "body": "Documentation update",
            "labels": [{"name": "documentation"}],
        }
        with patch("coord.github_ops.get_issue", return_value=issue_payload), \
             patch("coord.dispatch.dispatch", return_value={"id": "doc-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "20", "--config", str(pipeline_config_file)],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.required_gates == ["merge"]

    def test_hotfix_label_resolves_to_merge_only(
        self, pipeline_config_file: Path, coord_dir: Path
    ) -> None:
        issue_payload = {
            "title": "Hotfix auth",
            "body": "",
            "labels": [{"name": "hotfix"}],
        }
        with patch("coord.github_ops.get_issue", return_value=issue_payload), \
             patch("coord.dispatch.dispatch", return_value={"id": "hf-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "21", "--config", str(pipeline_config_file)],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.required_gates == ["merge"]

    def test_needs_smoke_label_resolves_to_full_pipeline(
        self, pipeline_config_file: Path, coord_dir: Path
    ) -> None:
        issue_payload = {
            "title": "Big feature",
            "body": "",
            "labels": [{"name": "needs-smoke"}],
        }
        with patch("coord.github_ops.get_issue", return_value=issue_payload), \
             patch("coord.dispatch.dispatch", return_value={"id": "ns-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "22", "--config", str(pipeline_config_file)],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.required_gates == ["review", "smoke", "merge"]

    def test_unrecognized_label_falls_back_to_default_gates(
        self, pipeline_config_file: Path, coord_dir: Path
    ) -> None:
        """Labels not in pipeline.labels fall back to pipeline.default_gates."""
        issue_payload = {
            "title": "Fix bug",
            "body": "",
            "labels": [{"name": "bug"}],
        }
        with patch("coord.github_ops.get_issue", return_value=issue_payload), \
             patch("coord.dispatch.dispatch", return_value={"id": "bug-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "23", "--config", str(pipeline_config_file)],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        # No matching label → default_gates from config
        assert proposal.required_gates == ["review", "merge"]

    def test_no_labels_falls_back_to_default_gates(
        self, pipeline_config_file: Path, coord_dir: Path
    ) -> None:
        """Issue with no labels falls back to pipeline.default_gates."""
        issue_payload = {"title": "Unlabeled issue", "body": "", "labels": []}
        with patch("coord.github_ops.get_issue", return_value=issue_payload), \
             patch("coord.dispatch.dispatch", return_value={"id": "nolabel-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "24", "--config", str(pipeline_config_file)],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.required_gates == ["review", "merge"]


class TestAssignFreshness:
    """#267: coord assign must run the dependency freshness check that
    coord approve already does — otherwise TUI right-click → Start
    silently dispatches against stale dep checkouts."""

    @pytest.fixture
    def config_with_dep(self, tmp_path: Path) -> Path:
        """Config where `api` depends on `lib` so freshness has something
        non-trivial to compare against."""
        p = tmp_path / "coordinator.yml"
        p.write_text("""\
repos:
  - name: lib
    github: acme/lib
    default_branch: main
  - name: api
    github: acme/api
    default_branch: main
    depends_on: [lib]
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api, lib]
    repo_paths:
      api: /tmp/api
      lib: /tmp/lib
""")
        return p

    def test_freshness_pulls_stale_dep_by_default(
        self, config_with_dep: Path, coord_dir: Path
    ) -> None:
        """Default behaviour: stale dep triggers an auto-pull on dispatch."""
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "f-1"}) as mock_dispatch, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch(
                 "coord.network.fetch_repos",
                 return_value={"lib": {"sha": "OLD", "branch": "main", "dirty": False}},
             ), \
             patch(
                 "coord.github_ops.get_default_branch_head",
                 side_effect=lambda repo, branch: "NEW" if "lib" in repo else "NEW2",
             ):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_with_dep)],
            )
        assert result.exit_code == 0, result.output
        # The freshness output names the stale dep.
        assert "dependency lib: stale" in result.output
        # And auto-pull is the default, so pull_repos is set.
        _, kwargs = mock_dispatch.call_args
        assert "lib" in kwargs.get("pull_repos", [])

    def test_no_pull_flag_emits_briefing_addendum_instead(
        self, config_with_dep: Path, coord_dir: Path
    ) -> None:
        """--no-pull leaves the briefing carrying a 'pull these' addendum
        but doesn't request the agent to pull."""
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "f-1"}) as mock_dispatch, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch(
                 "coord.network.fetch_repos",
                 return_value={"lib": {"sha": "OLD", "branch": "main", "dirty": False}},
             ), \
             patch(
                 "coord.github_ops.get_default_branch_head",
                 return_value="NEW",
             ):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_with_dep), "--no-pull"],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_dispatch.call_args
        assert kwargs.get("pull_repos") == []
        proposal = mock_dispatch.call_args[0][0]
        assert "Stale dependencies" in proposal.briefing

    def test_skip_freshness_flag_bypasses_check_entirely(
        self, config_with_dep: Path, coord_dir: Path
    ) -> None:
        """--skip-freshness should make no network calls for HEADs or repo
        states — fastest path, used for hot-path / offline dispatches."""
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "f-1"}) as mock_dispatch, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.network.fetch_repos") as fetch, \
             patch("coord.github_ops.get_default_branch_head") as get_head:
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1",
                 "--config", str(config_with_dep), "--skip-freshness"],
            )
        assert result.exit_code == 0, result.output
        fetch.assert_not_called()
        get_head.assert_not_called()
        _, kwargs = mock_dispatch.call_args
        assert kwargs.get("pull_repos") == []


def _seed_done_work(assignment_id: str, branch: str) -> None:
    """Persist a completed work assignment (with a branch) so the interactive
    review path can resolve it via build_board().find_by_id()."""
    from coord.models import Assignment, Board, Repo
    from coord.state import save_board

    work = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="Fix bug",
        assignment_id=assignment_id,
        status="done",
        branch=branch,
        type="work",
        dispatched_at=0.0,
        finished_at=1.0,
    )
    board = Board(
        repos=[Repo(name="api", github="acme/api")],
        machines=[],
        active=[],
        completed=[work],
    )
    save_board(board)


class TestAssignInteractiveReview:
    """A1: `coord assign --interactive --review-of <work_aid>` — launch a
    human-attended interactive REVIEW linked to completed work."""

    def test_review_of_requires_interactive(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--review-of", "work-123"],
            )
        assert result.exit_code == 2
        assert "--review-of requires --interactive" in result.output

    def test_review_of_unknown_work_errors(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--review-of", "does-not-exist", "--dry-run"],
            )
        assert result.exit_code == 2
        assert "no such assignment" in result.output

    def test_review_of_dry_run_builds_review_dispatch(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        _seed_done_work("work-abc", "issue-1-fix-bug")
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--review-of", "work-abc", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        # Review-shaped, on the work's branch, in the live checkout (no worktree).
        assert "REVIEW of #1" in result.output
        assert "issue-1-fix-bug" in result.output
        assert "live checkout" in result.output
        assert "(dry run — not launched)" in result.output
        # Dry-run must NOT record a review row.
        from coord.state import build_board
        assert build_board().find_by_id("work-abc") is not None  # work still there
        review_rows = [
            a for a in build_board().completed + build_board().active
            if a.type == "review" and a.review_of_assignment_id == "work-abc"
        ]
        assert review_rows == [], "dry-run must not persist a review assignment"

    def test_review_of_remote_dry_run_builds_remote_dispatch(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Track B / #486: a remote `--review-of` is no longer gated; the
        dry-run shows the read-only ssh+tmux dispatch (remote checkout, no
        worktree, absolute claude path) instead of the old local-only error."""
        _seed_done_work("work-abc", "issue-1-fix-bug")
        # gethostname=laptop ⇒ machine "server" resolves as REMOTE.
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "server", "api", "1", "--config", str(config_file),
                 "--interactive", "--review-of", "work-abc", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        # The local-only gate is gone.
        assert "local-only" not in result.output
        # Remote-shaped: ssh+tmux, read-only live checkout, no worktree.
        assert "remote tmux" in result.output
        assert "remote checkout" in result.output
        # The live checkout (configured repo_path), read-only, no worktree.
        assert "/tmp/api" in result.output
        assert "read-only, no worktree" in result.output
        assert "Track B #486" in result.output
        # Absolute remote claude binary (not on the SSH login PATH).
        assert "~/.local/bin/claude" in result.output
        assert "(dry run — not launched)" in result.output
        # Dry-run must NOT persist a review row.
        from coord.state import build_board
        review_rows = [
            a for a in build_board().completed + build_board().active
            if a.type == "review" and a.review_of_assignment_id == "work-abc"
        ]
        assert review_rows == [], "dry-run must not persist a review assignment"

    def test_review_of_remote_session_ended_finalizes(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Track B / #486: when the remote review tmux session has ENDED, the
        coordinator must record a terminal state via finalize_interactive_exit
        (worktree_path=None, repo_path=None) — otherwise the review row lingers
        as a phantom 'running' worker holding the claim forever.  Read-only
        review ⇒ no worktree/no repo_path so the backstop only writes the DB."""
        from unittest.mock import MagicMock

        _seed_done_work("work-abc", "issue-1-fix-bug")
        fake_result = MagicMock(already_recorded=False, terminal_status="advisory")
        finalize_spy = MagicMock(return_value=fake_result)
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"), \
             patch("coord.interactive._launch_via_tmux", return_value=0), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_interactive_exit", finalize_spy):
            result = CliRunner().invoke(
                main,
                ["assign", "server", "api", "1", "--config", str(config_file),
                 "--interactive", "--review-of", "work-abc"],
            )
        assert result.exit_code == 0, result.output
        # The backstop fired with the read-only (no-worktree) signature.
        assert finalize_spy.call_count == 1, "finalize must run on session-end"
        kwargs = finalize_spy.call_args.kwargs
        assert kwargs["worktree_path"] is None
        assert kwargs["repo_path"] is None
        assert kwargs["assignment_id"]  # the recorded review id
        # #486d: non-TTY (CliRunner) → the inline verdict prompt is skipped and
        # the manual `coord report-result` hint is printed instead.
        assert "no verdict reported" in result.output
        assert "coord report-result" in result.output

    def test_review_of_remote_session_alive_skips_finalize(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """When the remote review session is still DETACHED in tmux, finalize
        must NOT run (the row stays running deliberately, awaiting reattach +
        the operator's `coord report-result`)."""
        from unittest.mock import MagicMock

        _seed_done_work("work-abc", "issue-1-fix-bug")
        finalize_spy = MagicMock()
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"), \
             patch("coord.interactive._launch_via_tmux", return_value=0), \
             patch("coord.interactive.tmux_session_alive", return_value=True), \
             patch("coord.interactive.finalize_interactive_exit", finalize_spy):
            result = CliRunner().invoke(
                main,
                ["assign", "server", "api", "1", "--config", str(config_file),
                 "--interactive", "--review-of", "work-abc"],
            )
        assert result.exit_code == 0, result.output
        assert finalize_spy.call_count == 0, "alive session must not finalize"
        assert "session still running in remote tmux" in result.output
        assert "coord report-result" in result.output

    def test_review_of_local_session_ended_prompts_for_verdict(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """A LOCAL interactive review that exits without the reviewer running
        `coord report-result` must route the missing verdict through the
        operator prompt+relay helper (parity with the remote #486d path) —
        not silently print 'no verdict reported' and strand the merge gate.

        The prompt helper is spied here; when it declines (returns False, e.g.
        the non-TTY CliRunner) the stall consequence is still surfaced."""
        from unittest.mock import MagicMock

        _seed_done_work("work-abc", "issue-1-fix-bug")
        fake_result = MagicMock(already_recorded=False, terminal_status="advisory")
        finalize_spy = MagicMock(return_value=fake_result)
        relay_spy = MagicMock(return_value=False)
        local_spy = MagicMock(return_value=0)
        remote_spy = MagicMock()
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"), \
             patch("coord.interactive.launch_human_attended_interactive", local_spy), \
             patch("coord.interactive._launch_via_tmux", remote_spy), \
             patch("coord.interactive.tmux_available", return_value=False), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_interactive_exit", finalize_spy), \
             patch("coord.cli._prompt_and_relay_review_verdict", relay_spy):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--review-of", "work-abc"],
            )
        assert result.exit_code == 0, result.output
        # Local path was taken (not the remote tmux launch).
        local_spy.assert_called_once()
        remote_spy.assert_not_called()
        # The fix: the local exit routes the missing verdict through the
        # operator prompt+relay helper (parity with the remote #486d path).
        relay_spy.assert_called_once()
        kwargs = relay_spy.call_args.kwargs
        assert kwargs["assignment_id"]
        assert kwargs["repo_name"] == "api"
        assert kwargs["issue_number"] == 1
        assert "coord report-result" in kwargs["verdict_cmd_hint"]
        # Helper declined (False) ⇒ the stall consequence is still surfaced.
        assert "no verdict reported" in result.output
        assert "merge gate" in result.output


def _seed_review_and_work(
    work_id: str,
    review_id: str,
    branch: str,
    *,
    verdict: str = "request-changes",
    review_iteration: int = 0,
) -> None:
    """Persist a done work assignment + a linked review (with verdict) so the
    `--fix-of` path can resolve review → work → branch."""
    from coord.models import Assignment, Board, Repo
    from coord.state import save_board

    work = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="Fix bug",
        assignment_id=work_id,
        status="done",
        branch=branch,
        type="work",
        review_iteration=review_iteration,
        dispatched_at=0.0,
        finished_at=1.0,
    )
    review = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="[review] Fix bug",
        assignment_id=review_id,
        status="done",
        branch=branch,
        type="review",
        review_of_assignment_id=work_id,
        review_verdict=verdict,
        dispatched_at=2.0,
        finished_at=3.0,
    )
    board = Board(
        repos=[Repo(name="api", github="acme/api")],
        machines=[],
        active=[],
        completed=[work, review],
    )
    save_board(board)


class TestAssignInteractiveFix:
    """Leg 3 (#517): `coord assign --interactive --fix-of <review_aid>` —
    a human-attended fix continuing the reviewed work's branch."""

    def test_fix_of_requires_interactive(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--fix-of", "rev-123"],
            )
        assert result.exit_code == 2
        assert "--fix-of requires --interactive" in result.output

    def test_fix_of_mutually_exclusive_with_review_of(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--fix-of", "rev-1", "--review-of", "work-1"],
            )
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output

    def test_fix_of_unknown_review_errors(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--fix-of", "nope", "--dry-run"],
            )
        assert result.exit_code == 2
        assert "no such assignment" in result.output

    def test_fix_of_on_non_review_id_errors(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        # Passing the WORK id (type=work) instead of the review id must error.
        _seed_review_and_work("work-x", "rev-x", "issue-1-fix-bug")
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--fix-of", "work-x", "--dry-run"],
            )
        assert result.exit_code == 2
        assert "not 'review'" in result.output

    def test_fix_of_dry_run_continues_existing_branch(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        _seed_review_and_work("work-y", "rev-y", "issue-1-fix-bug")
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--fix-of", "rev-y", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        # Fix-shaped, iteration 1, continuing the work's branch.
        assert "FIX of #1" in result.output
        assert "iteration 1/" in result.output
        assert "would continue branch: issue-1-fix-bug" in result.output
        assert "(dry run — not launched)" in result.output
        # Dry-run must NOT persist a fix row.
        from coord.state import build_board
        b = build_board()
        fix_rows = [a for a in b.active + b.completed if a.review_iteration == 1]
        assert fix_rows == [], "dry-run must not persist a fix assignment"

    def test_fix_of_remote_dry_run_builds_remote_dispatch(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Track B / #486: a remote `--fix-of` is no longer gated; the dry-run
        shows the ssh+tmux dispatch (remote worktree on the existing branch,
        absolute claude path) instead of the old local-only error."""
        _seed_review_and_work("work-y", "rev-y", "issue-1-fix-bug")
        # gethostname=laptop ⇒ machine "server" resolves as REMOTE.
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "server", "api", "1", "--config", str(config_file),
                 "--interactive", "--fix-of", "rev-y", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "local-only" not in result.output
        assert "FIX of #1" in result.output
        assert "iteration 1/" in result.output
        assert "remote tmux" in result.output
        # A fix WRITES → a remote worktree on the existing branch (not the
        # read-only live checkout the review uses).
        assert "remote worktree: $HOME/.coord/worktrees/" in result.output
        assert "would continue branch: issue-1-fix-bug" in result.output
        assert "~/.local/bin/claude" in result.output
        assert "Track B #486" in result.output
        assert "(dry run — not launched)" in result.output
        from coord.state import build_board
        b = build_board()
        fix_rows = [a for a in b.active + b.completed if a.review_iteration == 1]
        assert fix_rows == [], "dry-run must not persist a fix assignment"


class TestAssignInteractiveRemoteWork:
    """#486d: a remote interactive WORK session pushes its commits back on
    session-end via finalize_remote_interactive_exit (was a deferred no-op)."""

    def test_remote_work_session_ended_pushes_back(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        from unittest.mock import MagicMock

        fake = MagicMock(
            already_recorded=False, terminal_status="done",
            commits_ahead=2, push_ok=True, push_error=None,
        )
        spy = MagicMock(return_value=fake)
        # gethostname=laptop ⇒ machine "server" resolves as REMOTE.
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"), \
             patch("coord.interactive._launch_via_tmux", return_value=0), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_remote_interactive_exit", spy):
            result = CliRunner().invoke(
                main,
                ["assign", "server", "api", "1", "--config", str(config_file),
                 "--interactive", "--no-plan", "--force"],
            )
        assert result.exit_code == 0, result.output
        spy.assert_called_once()
        kwargs = spy.call_args.kwargs
        assert kwargs["ssh_target"]  # the remote machine's host
        assert "issue-1" in kwargs["branch"], "pushes the fresh work branch"
        assert "remote backstop" in result.output

    def test_remote_work_session_alive_skips_finalize(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        from unittest.mock import MagicMock

        spy = MagicMock()
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"), \
             patch("coord.interactive._launch_via_tmux", return_value=0), \
             patch("coord.interactive.tmux_session_alive", return_value=True), \
             patch("coord.interactive.finalize_remote_interactive_exit", spy):
            result = CliRunner().invoke(
                main,
                ["assign", "server", "api", "1", "--config", str(config_file),
                 "--interactive", "--no-plan", "--force"],
            )
        assert result.exit_code == 0, result.output
        spy.assert_not_called()
        assert "coord reattach" in result.output
