"""Managed Agent session lifecycle for worker agents."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from anthropic import Anthropic

from board import Assignment


@dataclass
class AgentSetup:
    agent_id: str
    agent_version: int
    environment_id: str


def get_client() -> Anthropic:
    return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def create_agent(client: Anthropic, system_prompt: str) -> tuple[str, int]:
    agent = client.beta.agents.create(
        name="vimcode-worker",
        model="claude-sonnet-4-6",
        system=system_prompt,
        tools=[{"type": "agent_toolset_20260401"}],
    )
    return agent.id, agent.version


def create_environment(client: Anthropic) -> str:
    env = client.beta.environments.create(
        name="vimcode-worker-env",
        config={
            "type": "cloud",
            "networking": {"type": "unrestricted"},
        },
    )
    return env.id


def fire_worker(
    client: Anthropic,
    setup: AgentSetup,
    repo: str,
    assignment: Assignment,
) -> str:
    """Start a Managed Agent session for one assignment. Returns session_id."""
    github_token = os.environ["GITHUB_TOKEN"]

    session = client.beta.sessions.create(
        agent=setup.agent_id,
        environment_id=setup.environment_id,
        title=f"#{assignment.issue_number} — {assignment.issue_title[:50]}",
        resources=[
            {
                "type": "github_repository",
                "url": f"https://github.com/{repo}",
                "mount_path": "/workspace/repo",
                "authorization_token": github_token,
                "checkout": {"type": "branch", "name": "develop"},
            },
        ],
    )

    forbidden = ", ".join(assignment.files_forbidden) if assignment.files_forbidden else "none"
    message = (
        f"Work on issue #{assignment.issue_number}: {assignment.issue_title}\n\n"
        f"Read the latest comment on the issue for implementation notes.\n\n"
        f"DO NOT TOUCH these files (other agents are working there): {forbidden}\n\n"
        f"{assignment.briefing}"
    )

    client.beta.sessions.events.send(
        session.id,
        events=[
            {
                "type": "user.message",
                "content": [{"type": "text", "text": message}],
            },
        ],
    )

    return session.id


def poll_worker(client: Anthropic, session_id: str) -> str:
    """Check session status. Returns 'running' | 'idle'."""
    session = client.beta.sessions.retrieve(session_id)
    return session.status


def stream_worker_events(client: Anthropic, session_id: str):
    """Yield events from the worker session for live monitoring."""
    with client.beta.sessions.events.stream(session_id) as stream:
        for event in stream:
            yield event


def get_worker_summary(client: Anthropic, session_id: str) -> str:
    """Extract the last agent message as a summary of what was done."""
    events = client.beta.sessions.events.list(session_id)
    for event in reversed(events.data):
        if event.type == "agent.message":
            for block in event.content:
                if hasattr(block, "text"):
                    return block.text
    return "(no summary available)"


def wait_for_any_completion(
    client: Anthropic,
    sessions: dict[str, str],
    poll_interval: int = 30,
) -> list[str]:
    """Poll until at least one session goes idle. Returns list of completed worker names."""
    while True:
        completed = []
        for worker_name, session_id in sessions.items():
            status = poll_worker(client, session_id)
            if status == "idle":
                completed.append(worker_name)
        if completed:
            return completed
        time.sleep(poll_interval)
