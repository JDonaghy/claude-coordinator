"""Dispatch approved assignments to agent servers and post briefings."""

from __future__ import annotations

import time
from typing import Iterable

import httpx

from coord import github_ops
from coord.comments import (
    format_briefing,
    format_completion,
    format_failure,
)
from coord.config import Config
from coord.models import Proposal

AGENT_PORT = 7433


def dispatch(
    proposal: Proposal,
    config: Config,
    *,
    pull_repos: Iterable[str] = (),
) -> dict:
    """POST an assignment to the agent server on the target machine.

    Returns the response JSON from the agent server (which includes the
    server-assigned `id`).
    """
    machine = next(
        (m for m in config.machines if m.name == proposal.machine_name), None
    )
    if machine is None:
        raise ValueError(f"Unknown machine: {proposal.machine_name!r}")

    repo_path = machine.repo_path(proposal.repo_name)
    if repo_path is None:
        raise ValueError(
            f"No repo_path configured for {proposal.repo_name!r} on machine {machine.name!r}. "
            f"Add it to coordinator.yml under machines[].repo_paths."
        )

    url = f"http://{machine.host}:{AGENT_PORT}/assign"
    payload = {
        "repo_name": proposal.repo_name,
        "repo_path": repo_path,
        "issue_number": proposal.issue_number,
        "issue_title": proposal.issue_title,
        "briefing": proposal.briefing,
        "files_allowed": proposal.files_likely,
        "files_forbidden": [],
        "pull_repos": list(pull_repos),
    }

    resp = httpx.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


def dispatch_with_retry(
    proposal: Proposal,
    config: Config,
    *,
    max_retries: int = 3,
    backoff_base: float = 60.0,
    pull_repos: Iterable[str] = (),
    on_retry: callable | None = None,
) -> dict:
    """Dispatch with exponential backoff on transient failures."""
    from coord.network import classify_error, is_retryable

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return dispatch(proposal, config, pull_repos=pull_repos)
        except httpx.HTTPError as exc:
            state, reason = classify_error(exc)
            if not is_retryable(state) or attempt == max_retries:
                raise
            wait = backoff_base * (2 ** attempt)
            if on_retry:
                on_retry(attempt + 1, max_retries, state, reason, wait)
            time.sleep(wait)
            last_exc = exc
        except ValueError:
            raise
    raise last_exc  # unreachable, but satisfies type checker


def compute_do_not_touch(
    proposal: Proposal,
    peers: Iterable[Proposal],
    in_flight: Iterable[dict] = (),
) -> list[tuple[str, str]]:
    """Compute (file, reason) pairs for other work touching `proposal.repo_name`.

    `peers` are other proposals being dispatched in the same batch.
    `in_flight` are records loaded from ~/.coord/dispatched.json (each with
    keys: machine_name, repo_name, files_likely).
    """
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(machine_name: str, files: Iterable[str]) -> None:
        for f in files:
            key = (machine_name, f)
            if key in seen:
                continue
            seen.add(key)
            pairs.append((f, f"{machine_name} is working there"))

    for peer in peers:
        if peer is proposal:
            continue
        if peer.repo_name != proposal.repo_name:
            continue
        _add(peer.machine_name, peer.files_likely)

    for record in in_flight:
        if record.get("repo_name") != proposal.repo_name:
            continue
        if record.get("machine_name") == proposal.machine_name:
            continue
        _add(record.get("machine_name", "?"), record.get("files_likely", []))

    return pairs


def post_briefing(
    proposal: Proposal,
    config: Config,
    *,
    assignment_id: str = "pending",
    do_not_touch: Iterable[tuple[str, str]] = (),
) -> None:
    """Post the assignment briefing as a GitHub issue comment."""
    repo = config.repo(proposal.repo_name)
    if repo is None:
        raise ValueError(f"Unknown repo: {proposal.repo_name!r}")

    body = format_briefing(
        assignment_id=assignment_id,
        machine_name=proposal.machine_name,
        repo_name=proposal.repo_name,
        issue_number=proposal.issue_number,
        briefing=proposal.briefing,
        files_likely=proposal.files_likely,
        do_not_touch=do_not_touch,
    )
    github_ops.post_issue_comment(repo.github, proposal.issue_number, body)


def post_completion(
    *,
    assignment_id: str,
    machine_name: str,
    repo_github: str,
    repo_name: str,
    issue_number: int,
    exit_code: int,
    duration_seconds: float | None = None,
    log_path: str | None = None,
    summary: str = "",
) -> None:
    body = format_completion(
        assignment_id=assignment_id,
        machine_name=machine_name,
        repo_name=repo_name,
        issue_number=issue_number,
        exit_code=exit_code,
        duration_seconds=duration_seconds,
        log_path=log_path,
        summary=summary,
    )
    github_ops.post_issue_comment(repo_github, issue_number, body)


def post_failure(
    *,
    assignment_id: str,
    machine_name: str,
    repo_github: str,
    repo_name: str,
    issue_number: int,
    exit_code: int | None,
    duration_seconds: float | None = None,
    log_path: str | None = None,
    error: str = "",
) -> None:
    body = format_failure(
        assignment_id=assignment_id,
        machine_name=machine_name,
        repo_name=repo_name,
        issue_number=issue_number,
        exit_code=exit_code,
        duration_seconds=duration_seconds,
        log_path=log_path,
        error=error,
    )
    github_ops.post_issue_comment(repo_github, issue_number, body)
