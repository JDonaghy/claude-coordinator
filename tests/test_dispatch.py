"""Tests for coord.dispatch — assignment dispatch and briefing."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from coord.config import Config
from coord.dispatch import dispatch, post_briefing
from coord.models import Machine, Proposal, Repo


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[
            Repo(name="api", github="acme/api"),
        ],
        machines=[
            Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            ),
        ],
    )


@pytest.fixture
def proposal() -> Proposal:
    return Proposal(
        id=1,
        machine_name="laptop",
        repo_name="api",
        issue_number=10,
        issue_title="Fix auth",
        rationale="best fit",
        files_likely=["auth.py"],
        briefing="Fix the auth module",
    )


class TestDispatch:
    @patch("coord.dispatch.httpx.post")
    def test_posts_to_agent_server(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        result = dispatch(proposal, config)
        assert result == {"ok": True}
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert "laptop.tailnet" in call_args.args[0]
        payload = call_args.kwargs["json"]
        assert payload["issue_number"] == 10
        assert payload["repo_path"] == "/home/user/src/api"
        assert payload["files_allowed"] == ["auth.py"]
        assert "files_likely" not in payload

    @patch("coord.dispatch.httpx.post")
    def test_payload_carries_default_branch(
        self, mock_post: MagicMock, proposal: Proposal,
    ) -> None:
        """#255: the dispatch payload must include the repo's configured
        default_branch so the agent doesn't fall back to a hardcoded "main"
        and silently route around `default_branch: develop` repos."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api", default_branch="develop")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        dispatch(proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["branch"] == "develop", (
            f"#255: expected branch=develop in payload, got {payload.get('branch')!r}"
        )

    @patch("coord.dispatch.httpx.post")
    def test_payload_branch_falls_back_to_main_when_unset(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        """When a repo doesn't specify default_branch, the payload still
        carries an explicit "main" so the agent never sees branch=None."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp
        dispatch(proposal, config)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["branch"] == "main"

    @patch("coord.dispatch.httpx.post")
    def test_payload_carries_target_branch_when_set(
        self, mock_post: MagicMock, config: Config,
    ) -> None:
        """When proposal.target_branch is set, dispatch payload includes it
        so the agent checks out the explicit branch instead of slugifying the
        (possibly `[fix-N] …` / `[conflict-fix] …`-prefixed) issue title."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=206, issue_title="[fix-1] tui machines panel restart update",
            rationale="follow-up",
            target_branch="issue-206-tui-machines-panel-restart-update",
        )
        dispatch(p, config)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["target_branch"] == "issue-206-tui-machines-panel-restart-update"

    @patch("coord.dispatch.httpx.post")
    def test_payload_omits_target_branch_when_unset(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        """Older agents (pre-#target_branch) reject unknown kwargs in
        AssignmentSpec(**body), so the field must be omitted when not set."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp
        dispatch(proposal, config)
        payload = mock_post.call_args.kwargs["json"]
        assert "target_branch" not in payload

    def test_unknown_machine_raises(self, config: Config) -> None:
        bad = Proposal(
            id=1, machine_name="ghost", repo_name="api",
            issue_number=1, issue_title="x", rationale="",
        )
        with pytest.raises(ValueError, match="Unknown machine"):
            dispatch(bad, config)

    def test_missing_repo_path_raises(self) -> None:
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="h", repos=["api"])],
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="x", rationale="",
        )
        with pytest.raises(ValueError, match="repo_path"):
            dispatch(p, cfg)


class TestPostBriefing:
    @patch("coord.dispatch.github_ops.post_issue_comment")
    def test_posts_comment(
        self, mock_comment: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        post_briefing(proposal, config)
        mock_comment.assert_called_once()
        args = mock_comment.call_args.args
        assert args[0] == "acme/api"
        assert args[1] == 10
        assert "laptop" in args[2]
        assert "auth.py" in args[2]

    def test_unknown_repo_raises(self, config: Config) -> None:
        bad = Proposal(
            id=1, machine_name="laptop", repo_name="ghost",
            issue_number=1, issue_title="x", rationale="",
        )
        with pytest.raises(ValueError, match="Unknown repo"):
            post_briefing(bad, config)

    @patch("coord.dispatch.github_ops.add_issue_labels")
    @patch("coord.dispatch.github_ops.post_issue_comment")
    def test_auto_labels_issue_with_tracked_labels(
        self,
        mock_comment: MagicMock,
        mock_add_labels: MagicMock,
        config: Config,
        proposal: Proposal,
    ) -> None:
        """post_briefing must tag the issue with cfg.pipeline.tracked_labels()
        so the TUI Pipeline panel picks it up.  Without this, manually
        filed issues stay invisible until the user remembers to label them
        (we hit this on quadraui#263)."""
        post_briefing(proposal, config)
        mock_add_labels.assert_called_once_with("acme/api", 10, ["coord"])

    @patch("coord.dispatch.github_ops.add_issue_labels")
    @patch("coord.dispatch.github_ops.post_issue_comment")
    def test_labeling_failure_does_not_break_briefing(
        self,
        mock_comment: MagicMock,
        mock_add_labels: MagicMock,
        config: Config,
        proposal: Proposal,
    ) -> None:
        """Labeling is best-effort — a `gh` failure must not propagate
        and break the briefing flow."""
        mock_add_labels.side_effect = RuntimeError("gh not installed")
        post_briefing(proposal, config)  # must not raise
        mock_comment.assert_called_once()
        mock_add_labels.assert_called_once()


class TestResumeSessionId:
    """#315: resume_session_id flows from Proposal through dispatch payload."""

    @patch("coord.dispatch.httpx.post")
    def test_payload_carries_resume_session_id_when_set(
        self, mock_post: MagicMock, config: Config,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Chat",
            rationale="continuation",
            type="refinement",
            resume_session_id="ses-abc-123",
        )
        dispatch(p, config)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["resume_session_id"] == "ses-abc-123"

    @patch("coord.dispatch.httpx.post")
    def test_payload_omits_resume_session_id_when_unset(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        """Older agents reject unknown keys — the field must be absent when None."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, config)
        payload = mock_post.call_args.kwargs["json"]
        assert "resume_session_id" not in payload


class TestArtifactPaths:
    """#305: artifact_paths flows from repo config through dispatch payload."""

    @patch("coord.dispatch.httpx.post")
    def test_payload_carries_artifact_paths_for_work_assignment(
        self, mock_post: MagicMock,
    ) -> None:
        """Dispatch payload for a work proposal should include the repo's
        artifact_paths so remote agents can stash artifacts without coordinator.yml."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(
                name="api",
                github="acme/api",
                artifact_paths=["target/debug/mybinary*", "dist/*.tar.gz"],
            )],
            machines=[Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        p = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Build release",
            rationale="build",
            type="work",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["artifact_paths"] == ["target/debug/mybinary*", "dist/*.tar.gz"]

    @patch("coord.dispatch.httpx.post")
    def test_payload_includes_empty_artifact_paths_for_work_when_not_configured(
        self, mock_post: MagicMock,
    ) -> None:
        """Dispatch payload should include empty artifact_paths for work
        assignments when the repo has no artifact_paths configured."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],  # no artifact_paths
            machines=[Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        p = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Fix bug",
            rationale="fix",
            type="work",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["artifact_paths"] == []

    @patch("coord.dispatch.httpx.post")
    def test_payload_excludes_artifact_paths_for_review_assignment(
        self, mock_post: MagicMock,
    ) -> None:
        """Dispatch payload for a review proposal should have empty artifact_paths
        since reviews don't build artifacts."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(
                name="api",
                github="acme/api",
                artifact_paths=["target/debug/mybinary*"],
            )],
            machines=[Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        p = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Review PR",
            rationale="review",
            type="review",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["artifact_paths"] == []
