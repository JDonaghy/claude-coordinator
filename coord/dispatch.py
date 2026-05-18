"""Dispatch approved assignments to agent servers and post briefings."""

from __future__ import annotations

import httpx

from coord.config import Config
from coord.models import Proposal
from coord import github_ops

AGENT_PORT = 7433


def dispatch(proposal: Proposal, config: Config) -> dict:
    """POST an assignment to the agent server on the target machine.

    Returns the response JSON from the agent server.
    """
    machine = next(
        (m for m in config.machines if m.name == proposal.machine_name), None
    )
    if machine is None:
        raise ValueError(f"Unknown machine: {proposal.machine_name!r}")

    url = f"http://{machine.host}:{AGENT_PORT}/assign"
    payload = {
        "repo_name": proposal.repo_name,
        "issue_number": proposal.issue_number,
        "issue_title": proposal.issue_title,
        "briefing": proposal.briefing,
        "files_likely": proposal.files_likely,
    }

    resp = httpx.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


def post_briefing(proposal: Proposal, config: Config) -> None:
    """Post the assignment briefing as a GitHub issue comment."""
    repo = config.repo(proposal.repo_name)
    if repo is None:
        raise ValueError(f"Unknown repo: {proposal.repo_name!r}")

    body = (
        f"**Assignment dispatched to `{proposal.machine_name}`**\n\n"
        f"{proposal.briefing}\n\n"
        f"*Files likely touched:* {', '.join(f'`{f}`' for f in proposal.files_likely) or '(unspecified)'}"
    )
    github_ops.post_issue_comment(repo.github, proposal.issue_number, body)
