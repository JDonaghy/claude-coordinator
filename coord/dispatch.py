"""Dispatch approved assignments to agent servers and post briefings."""

from __future__ import annotations

import time
from typing import Iterable

import httpx

from coord import github_ops
from coord.comments import (
    format_advisory,
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
    fresh_branch: bool = False,
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

    # Resolve deny-list from the repo's worker_permissions config.
    repo = config.repo(proposal.repo_name)

    # #437: STRUCTURAL TOS-COMPLIANCE GATE — refuse to route an
    # unattended dispatch through a provider whose capabilities mark it
    # ``human_attended_only`` (subscription-billed interactive Claude
    # Code).  Precedence: per-proposal override (if the brain ever sets
    # one) → per-repo ``Repo.provider`` → ``config.providers.default``.
    # Deferred import so the unattended dispatch surface stays free of a
    # module-level cycle with the provider registry.
    from coord.providers import guard_unattended_dispatch  # noqa: PLC0415
    spec_provider = getattr(proposal, "provider", None)
    # #324: resolve the effective provider name (spec > repo > default) so
    # the coordinator DB always records the winning provider regardless of
    # which level supplied it, and the wire payload carries the exact name
    # the agent should look up in its registry.
    effective_provider_name: str = guard_unattended_dispatch(
        spec_provider=spec_provider,
        repo_provider=repo.provider if repo is not None else None,
        providers_cfg=config.providers,
        models_cfg=config.models,
        where="coord approve / dispatch",
    )
    deny_commands: list[str] = []
    if repo is not None and repo.worker_permissions is not None:
        deny_commands = repo.worker_permissions.deny

    # Resolve coordinator-only files (workers must not read or modify these).
    files_forbidden: list[str] = []
    if repo is not None and repo.coordinator_only_files:
        files_forbidden = list(repo.coordinator_only_files)

    # Resolve model: proposal override → config default → None (let claude pick).
    # The board/DB stores the alias for legibility; only the wire payload is
    # translated to an exact model id via models.versions (when configured).
    model = proposal.model if proposal.model else config.models.default
    wire_model = config.models.resolve(model)

    # #255: pin the worker's branch base to the repo's configured default
    # branch.  Without this the agent fell back to a hardcoded "main", which
    # silently routed around `default_branch: develop` repos like quadraui
    # and let local-only commits on the default branch slip into worker
    # branches.
    default_branch = (repo.default_branch if repo is not None else None) or "main"

    # #305: artifact_paths are only relevant for work assignments.  Skip for
    # review, smoke, refinement, and other non-work types.
    artifact_paths: list[str] = []
    if proposal.type == "work" and repo is not None:
        artifact_paths = list(repo.artifact_paths)

    # #352: resolve new-issue guidance for new-issue-chat assignments.
    # Only resolve when the repo *explicitly configured* new_issue_guidance —
    # the resolver always returns a non-empty _DEFAULT, so checking
    # `if new_issue_guidance:` below would always send the field, causing
    # agents that predate #352 to reject the payload with a 400.  Gating on
    # the raw config field lets repos without guidance dispatch to any agent
    # (the agent's built-in NEW_ISSUE_CHAT_SYSTEM_PROMPT is fine without it).
    new_issue_guidance: str = ""
    if proposal.type == "new-issue-chat" and repo is not None and repo.new_issue_guidance:
        from pathlib import Path
        new_issue_guidance = repo.resolve_new_issue_guidance(Path(repo_path).expanduser())

    url = f"http://{machine.host}:{AGENT_PORT}/assign"
    payload: dict = {
        "repo_name": proposal.repo_name,
        "repo_path": repo_path,
        "issue_number": proposal.issue_number,
        "issue_title": proposal.issue_title,
        "briefing": proposal.briefing,
        "files_allowed": proposal.files_likely,
        "files_forbidden": files_forbidden,
        "pull_repos": list(pull_repos),
        "deny_commands": deny_commands,
        "model": wire_model,
        "type": proposal.type,
        "branch": default_branch,
    }
    # #351: only send artifact_paths when non-empty — older agents reject
    # unknown payload keys with a 400.  When absent the agent falls back to
    # self.artifact_paths (startup config).
    if artifact_paths:
        payload["artifact_paths"] = artifact_paths
    # #352: only send new_issue_guidance when non-empty — older agents don't
    # have this field and will reject the payload with a 400.
    if new_issue_guidance:
        payload["new_issue_guidance"] = new_issue_guidance
    # Only send fresh_branch when True — older agents don't have this field
    # and will reject the payload with a 400.
    if fresh_branch:
        payload["fresh_branch"] = True
    # Only send target_branch when set — agents predating #target_branch
    # (and the AssignmentSpec(**body) kwargs check) reject unknown fields.
    if proposal.target_branch:
        payload["target_branch"] = proposal.target_branch
    # #315: only send resume_session_id when set — older agents without the
    # field reject unknown payload keys with a 400.
    if getattr(proposal, "resume_session_id", None):
        payload["resume_session_id"] = proposal.resume_session_id
    # #324: send the resolved provider name when it differs from the implicit
    # default ("claude").  Older agents that don't know about providers ignore
    # the field (AssignmentSpec(**body) accepts unknown kwargs since Python
    # 3.12 dataclasses don't reject extras — actually they DO reject extras,
    # but spec.provider was added in #425 so agents with that field already
    # accept it).  When the effective provider IS "claude", omitting the
    # field keeps the wire payload identical to pre-#324 for all default-
    # configured deployments (no-config parity requirement).
    if effective_provider_name and effective_provider_name != "claude":
        payload["provider"] = effective_provider_name

    resp = httpx.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    # #324: attach the resolved provider name to the response dict so callers
    # that record the dispatched assignment (cli.py, dashboard/server.py) can
    # persist it without re-resolving the config precedence chain.
    result["_provider_name"] = effective_provider_name
    return result


def dispatch_with_retry(
    proposal: Proposal,
    config: Config,
    *,
    max_retries: int = 3,
    backoff_base: float = 60.0,
    pull_repos: Iterable[str] = (),
    fresh_branch: bool = False,
    on_retry: callable | None = None,
) -> dict:
    """Dispatch with exponential backoff on transient failures."""
    from coord.network import classify_error, is_retryable

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return dispatch(proposal, config, pull_repos=pull_repos, fresh_branch=fresh_branch)
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

    # Auto-tag the issue with pipeline_tracked_labels so the TUI's Pipeline
    # panel picks it up on the next `gh search issues` poll.  Without this,
    # manually filed issues stay invisible until the user remembers to
    # label them (we hit this filing quadraui#263).  Best-effort — never
    # fail the briefing post on a labeling error.
    tracked = config.pipeline.tracked_labels()
    if tracked:
        try:
            github_ops.add_issue_labels(repo.github, proposal.issue_number, tracked)
        except (RuntimeError, OSError):
            pass


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


def post_advisory(
    *,
    assignment_id: str,
    machine_name: str,
    repo_github: str,
    repo_name: str,
    issue_number: int,
    duration_seconds: float | None = None,
    log_path: str | None = None,
    reason: str = "",
) -> None:
    body = format_advisory(
        assignment_id=assignment_id,
        machine_name=machine_name,
        repo_name=repo_name,
        issue_number=issue_number,
        duration_seconds=duration_seconds,
        log_path=log_path,
        reason=reason,
    )
    github_ops.post_issue_comment(repo_github, issue_number, body)
