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
            Machine(name="laptop", host="laptop.tailnet", repos=["api"]),
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
        assert call_args.kwargs["json"]["issue_number"] == 10

    def test_unknown_machine_raises(self, config: Config) -> None:
        bad = Proposal(
            id=1, machine_name="ghost", repo_name="api",
            issue_number=1, issue_title="x", rationale="",
        )
        with pytest.raises(ValueError, match="Unknown machine"):
            dispatch(bad, config)


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
