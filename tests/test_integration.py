"""End-to-end integration tests: brain → dispatch → agent → poll.

Mocks: claude -p subprocess, GitHub ops.
Real: agent server (HTTP via TestClient), dispatch, brain prompt/parse logic.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from starlette.testclient import TestClient

from coord.agent import AgentServer, AssignmentSpec, DONE, FAILED
from coord.agent_app import build_app
from coord.brain import build_prompt, gather_context, parse_proposals
from coord.config import Config
from coord.dispatch import dispatch, post_briefing
from coord.models import Machine, Proposal, Repo
from coord.state import save_proposals, load_proposals


def _init_repo(path: Path) -> Path:
    """Create a minimal git repo with one commit so worktrees can be created."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True, capture_output=True)
    (path / "README").write_text("init\n")
    subprocess.run(["git", "add", "README"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(path), check=True, capture_output=True)
    return path


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    return _init_repo(tmp_path / "repo")


@pytest.fixture
def agent_server(tmp_path: Path, repo_dir: Path) -> AgentServer:
    server = AgentServer(
        machine_name="testbox",
        capabilities=["python"],
        repos=["myapp"],
        state_dir=tmp_path / "agent_state",
        repo_paths={"myapp": str(repo_dir)},
        worker_command=lambda spec: ["/bin/sh", "-c", "echo 'worker done'; exit 0"],
    )
    yield server
    server.shutdown(kill_running=True)


@pytest.fixture
def agent_client(agent_server: AgentServer) -> TestClient:
    return TestClient(build_app(agent_server))


@pytest.fixture
def config(repo_dir: Path) -> Config:
    return Config(
        repos=[
            Repo(name="myapp", github="acme/myapp"),
        ],
        machines=[
            Machine(
                name="testbox",
                host="testbox.tailnet",
                capabilities=["python"],
                repos=["myapp"],
                repo_paths={"myapp": str(repo_dir)},
            ),
        ],
    )


@pytest.fixture
def sample_issues() -> list[dict]:
    return [
        {
            "number": 42,
            "title": "Fix login bug",
            "labels": [{"name": "bug"}],
            "body": "The login page crashes when password is empty.",
            "assignees": [],
            "milestone": None,
        },
        {
            "number": 43,
            "title": "Add user search",
            "labels": [{"name": "enhancement"}],
            "body": "Users should be able to search by name.",
            "assignees": [],
            "milestone": None,
        },
    ]


@pytest.fixture
def claude_response() -> str:
    return json.dumps([
        {
            "machine_name": "testbox",
            "repo_name": "myapp",
            "issue_number": 42,
            "issue_title": "Fix login bug",
            "rationale": "testbox is idle and has python",
            "files_likely": ["auth/login.py"],
            "briefing": "Fix the login crash when password is empty. Check auth/login.py for the validation logic.",
        },
    ])


# ── E2E: Full loop ─────────────────────────────────────────────────────────


class TestFullLoop:
    """Brain proposes → user approves → dispatch sends to agent → agent runs worker."""

    def test_plan_parse_dispatch_complete(
        self,
        tmp_path: Path,
        config: Config,
        agent_server: AgentServer,
        agent_client: TestClient,
        sample_issues: list[dict],
        claude_response: str,
        repo_dir: Path,
    ) -> None:
        # 1. Brain gathers context — mock GitHub, route agent HTTP through TestClient
        with (
            patch("coord.brain.github_ops.get_open_issues") as mock_issues,
            patch("coord.brain.httpx.get") as mock_get,
        ):
            mock_issues.return_value = sample_issues
            mock_get.return_value = MagicMock(
                json=lambda: agent_client.get("/status").json()
            )
            context = gather_context(config)

        assert "myapp" in context["issues_by_repo"]
        assert len(context["issues_by_repo"]["myapp"]) == 2

        # 2. Build prompt and verify it contains the right info
        prompt = build_prompt(config, context)
        assert "myapp" in prompt
        assert "#42" in prompt
        assert "Fix login bug" in prompt
        assert "testbox" in prompt

        # 3. Parse canned Claude response
        proposals = parse_proposals(claude_response)
        assert len(proposals) == 1
        assert proposals[0].machine_name == "testbox"
        assert proposals[0].issue_number == 42

        # 4. Save and reload proposals
        proposals_file = tmp_path / "proposals.json"
        with (
            patch("coord.state.COORD_DIR", tmp_path),
            patch("coord.state.PROPOSALS_FILE", proposals_file),
        ):
            save_proposals(proposals)
            loaded = load_proposals()
            assert len(loaded) == 1
            assert loaded[0].issue_number == 42

        # 5. Dispatch to real agent server via TestClient
        with patch("coord.dispatch.httpx.post") as mock_post:
            mock_post.return_value = MagicMock(
                json=lambda: agent_client.post(
                    "/assign",
                    json={
                        "repo_name": "myapp",
                        "repo_path": str(repo_dir),
                        "issue_number": 42,
                        "issue_title": "Fix login bug",
                        "briefing": proposals[0].briefing,
                        "files_allowed": ["auth/login.py"],
                        "files_forbidden": [],
                    },
                ).json(),
                raise_for_status=lambda: None,
            )
            result = dispatch(proposals[0], config)

        assert "id" in result

        # 6. Wait for the worker to finish
        assignment_id = result["id"]
        final = agent_server.wait_for(assignment_id)
        assert final.status == DONE
        assert final.exit_code == 0

        # 7. Verify log was written
        log_content = Path(final.log_path).read_text()
        assert "worker done" in log_content

        # 8. Agent status should show completed assignment
        status = agent_server.list_assignments()
        assert len(status["completed"]) == 1
        assert status["completed"][0]["id"] == assignment_id

    def test_dispatch_to_real_agent_http(
        self, config: Config, agent_server: AgentServer, agent_client: TestClient,
        repo_dir: Path,
    ) -> None:
        """Dispatch payload matches what the agent expects (AssignmentSpec shape)."""
        proposal = Proposal(
            id=1,
            machine_name="testbox",
            repo_name="myapp",
            issue_number=99,
            issue_title="Test issue",
            rationale="test",
            files_likely=["app.py"],
            briefing="Do the thing",
        )

        # Route dispatch HTTP through TestClient
        resp = agent_client.post("/assign", json={
            "repo_name": proposal.repo_name,
            "repo_path": str(repo_dir),
            "issue_number": proposal.issue_number,
            "issue_title": proposal.issue_title,
            "briefing": proposal.briefing,
            "files_allowed": proposal.files_likely,
            "files_forbidden": [],
        })
        assert resp.status_code == 202
        data = resp.json()
        assert data["spec"]["issue_number"] == 99
        assert data["spec"]["briefing"] == "Do the thing"

        agent_server.wait_for(data["id"])


# ── Agent status from brain ────────────────────────────────────────────────


class TestBrainAgentIntegration:
    """Brain's gather_context talks to agent's /status endpoint."""

    def test_idle_agent_detected(
        self, config: Config, agent_client: TestClient,
    ) -> None:
        with (
            patch("coord.brain.github_ops.get_open_issues", return_value=[]),
            patch("coord.brain.httpx.get") as mock_get,
        ):
            mock_get.return_value = MagicMock(
                json=lambda: agent_client.get("/status").json()
            )
            context = gather_context(config)

        status = context["machine_status"]["testbox"]
        assert "active" in status
        assert len(status["active"]) == 0

    def test_busy_agent_detected(
        self, config: Config, agent_server: AgentServer, agent_client: TestClient,
        repo_dir: Path,
    ) -> None:
        # Start a long-running assignment
        server_with_sleep = AgentServer(
            machine_name="testbox",
            capabilities=["python"],
            repos=["myapp"],
            state_dir=agent_server.state_dir.parent / "state2",
            repo_paths={"myapp": str(repo_dir)},
            worker_command=lambda spec: ["/bin/sh", "-c", "sleep 30"],
        )
        try:
            spec = AssignmentSpec(
                repo_name="myapp",
                repo_path=str(repo_dir),
                issue_number=1,
                issue_title="Long task",
                briefing="takes a while",
            )
            a = server_with_sleep.assign(spec)

            # Wait until running
            for _ in range(50):
                if server_with_sleep.get(a.id).status == "running":
                    break
                time.sleep(0.02)

            client = TestClient(build_app(server_with_sleep))
            status_resp = client.get("/status").json()
            assert len(status_resp["active"]) == 1
            assert status_resp["active"][0]["spec"]["issue_number"] == 1
        finally:
            server_with_sleep.shutdown(kill_running=True)


# ── Worker failure ──────────────────────────────────────────────────────────


class TestWorkerFailure:
    def test_failed_worker_reported_in_status(
        self, tmp_path: Path, repo_dir: Path,
    ) -> None:
        server = AgentServer(
            machine_name="testbox",
            repos=["myapp"],
            state_dir=tmp_path / "state",
            repo_paths={"myapp": str(repo_dir)},
            worker_command=lambda spec: ["/bin/sh", "-c", "echo 'error: something broke'; exit 1"],
        )
        try:
            spec = AssignmentSpec(
                repo_name="myapp",
                repo_path=str(repo_dir),
                issue_number=7,
                issue_title="Broken",
                briefing="this will fail",
            )
            a = server.assign(spec)
            final = server.wait_for(a.id)
            assert final.status == FAILED
            assert final.exit_code == 1

            log_content = Path(final.log_path).read_text()
            assert "something broke" in log_content
        finally:
            server.shutdown()


# ── Briefing post integration ───────────────────────────────────────────────


class TestBriefingIntegration:
    @patch("coord.dispatch.github_ops.post_issue_comment")
    def test_briefing_posted_with_correct_content(
        self, mock_comment: MagicMock, config: Config,
    ) -> None:
        proposal = Proposal(
            id=1,
            machine_name="testbox",
            repo_name="myapp",
            issue_number=42,
            issue_title="Fix login bug",
            rationale="test",
            files_likely=["auth/login.py", "tests/test_auth.py"],
            briefing="Fix the login crash",
        )
        post_briefing(proposal, config)

        mock_comment.assert_called_once()
        repo_slug, issue_num, body = mock_comment.call_args.args
        assert repo_slug == "acme/myapp"
        assert issue_num == 42
        assert "testbox" in body
        assert "Fix the login crash" in body
        assert "`auth/login.py`" in body
        assert "`tests/test_auth.py`" in body
