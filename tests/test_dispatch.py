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
