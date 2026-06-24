"""Coordinator brain — gathers context and calls the configured provider to propose assignments."""

from __future__ import annotations

import json
import re
import subprocess
import httpx
from typing import TYPE_CHECKING

from coord.config import Config
from coord.models import Proposal, SplitChunk, SplitProposal
from coord import github_ops

if TYPE_CHECKING:
    from coord.providers.base import Provider

AGENT_PORT = 7433

SYSTEM_PROMPT = """\
You are the coordinator brain for a multi-repo, multi-machine Claude Code system.

Your job: given a set of open issues and available machines, propose which machine
should work on which issue. Each machine runs one assignment at a time.

Rules:
- Only assign a machine to a repo it has in its repo list.
- Prefer to spread work across machines rather than queue on one.
- Respect repo dependencies: if repo A depends on repo B, and repo B has an open
  issue that blocks A's issue, assign B's issue first or flag the dependency.
- If two issues would touch overlapping files in the same repo, do NOT assign them
  simultaneously — flag the conflict and pick the higher-priority one.
- If a machine is already busy (has a running assignment), skip it.
- Write a concise briefing for each assignment: what the worker should do, which
  files are likely involved, and any constraints.

Split detection — if an issue is too large for a single worker session, propose
a split instead of an assignment. Signs an issue is too large:
- Issue body has a numbered/bulleted list with 5+ independent items
- Multiple independent files/surfaces/endpoints to change
- Title contains "migrate all", "replace remaining", "deduplicate all", etc.
Do NOT split issues that are naturally sequential or tightly coupled.

Respond with a JSON array. Each element is EITHER an assignment:
{
  "type": "assignment",
  "machine_name": "...",
  "repo_name": "...",
  "issue_number": 123,
  "issue_title": "...",
  "rationale": "why this machine for this issue",
  "files_likely": ["path/to/file.py", ...],
  "briefing": "worker instructions"
}

OR a split proposal:
{
  "type": "split",
  "repo_name": "...",
  "issue_number": 123,
  "issue_title": "...",
  "rationale": "why this issue should be split",
  "chunks": [
    {"title": "chunk title", "scope": "what this chunk covers", "files_likely": [...]},
    ...
  ]
}

If there is nothing to assign (no idle machines, no open issues, or all issues
are blocked), return an empty array: []

Respond with ONLY the JSON array — no markdown fences, no commentary.\
"""


def gather_context(config: Config) -> dict:
    """Fetch open issues per repo and agent status per machine."""
    from coord.state import upsert_open_issues

    issues_by_repo: dict[str, list[dict]] = {}
    for repo in config.repos:
        try:
            issues = github_ops.get_open_issues(repo.github)
            issues_by_repo[repo.name] = issues
            upsert_open_issues(repo.name, issues)
        except RuntimeError:
            issues_by_repo[repo.name] = []

    machine_status: dict[str, dict] = {}
    for machine in config.machines:
        try:
            resp = httpx.get(
                f"http://{machine.host}:{AGENT_PORT}/status",
                timeout=5,
            )
            machine_status[machine.name] = resp.json()
        except (httpx.HTTPError, httpx.TimeoutException):
            machine_status[machine.name] = {"status": "offline"}

    return {
        "issues_by_repo": issues_by_repo,
        "machine_status": machine_status,
    }


def build_prompt(config: Config, context: dict) -> str:
    """Assemble the user prompt from config and gathered context."""
    from coord.deps import blocked_repos
    from coord.models import Assignment

    lines: list[str] = []

    lines.append("## Repos")
    for repo in config.repos:
        deps = f" (depends on: {', '.join(repo.depends_on)})" if repo.depends_on else ""
        lines.append(f"- {repo.name} ({repo.github}){deps}")

    lines.append("")
    from coord.machine_pause import paused_set
    paused = paused_set()
    lines.append("## Machines")
    for machine in config.machines:
        caps = ", ".join(machine.capabilities) if machine.capabilities else "none"
        repos = ", ".join(machine.repos) if machine.repos else "none"
        status = context["machine_status"].get(machine.name, {})
        if machine.name in paused:
            # Routing-pause: do not propose work for this machine until
            # the user runs `coord unpause`.  Reachability is unchanged.
            state = "paused (do not propose work)"
        elif status.get("status") == "offline":
            state = "offline"
        elif status.get("assignment"):
            state = f"busy (working on: {status['assignment'].get('issue_title', '?')})"
        else:
            state = "idle"
        lines.append(f"- {machine.name} @ {machine.host} [{state}]")
        lines.append(f"  capabilities: {caps}")
        lines.append(f"  repos: {repos}")

    lines.append("")
    lines.append("## Open Issues")
    for repo_name, issues in context["issues_by_repo"].items():
        if not issues:
            lines.append(f"### {repo_name}: (no open issues)")
            continue
        lines.append(f"### {repo_name}")
        for issue in issues:
            labels = ", ".join(l.get("name", "") for l in issue.get("labels", []))
            label_str = f" [{labels}]" if labels else ""
            lines.append(f"- #{issue['number']}: {issue['title']}{label_str}")
            body = (issue.get("body") or "").strip()
            if body:
                preview = body[:150]
                if len(body) > 150:
                    preview += "..."
                lines.append(f"  {preview}")

    # Build active assignments from machine status to compute blocked repos
    active_assignments: list[Assignment] = []
    for machine_name, status in context["machine_status"].items():
        for entry in status.get("active", []):
            spec = entry.get("spec", {})
            active_assignments.append(Assignment(
                machine_name=machine_name,
                repo_name=spec.get("repo_name", ""),
                issue_number=spec.get("issue_number", 0),
                issue_title=spec.get("issue_title", ""),
                status="running",
            ))

    blocked = blocked_repos(config.repos, active_assignments)
    if blocked:
        lines.append("")
        lines.append("## Blocked Repos (DO NOT assign work here)")
        for repo_name, reasons in blocked.items():
            lines.append(f"### {repo_name} — BLOCKED")
            for reason in reasons:
                lines.append(f"  - {reason}")

    return "\n".join(lines)


def _resolve_default_provider(config: Config) -> "Provider":
    """Instantiate the coordinator's default provider from *config*.

    Uses the precedence chain ``providers.default → "claude"`` (no per-spec
    or per-repo override at the brain level — brain calls are
    coordinator-global).  Falls back to :class:`~coord.providers.claude.ClaudeProvider`
    when the resolved name is not found in ``providers.definitions``
    (shouldn't happen because ``ProvidersConfig.__post_init__`` always
    materialises the implicit ``"claude"`` entry).

    Args:
        config: The coordinator config.

    Returns:
        A ready-to-use :class:`~coord.providers.base.Provider` instance.
    """
    from coord.providers import build_provider  # noqa: PLC0415
    from coord.providers.claude import ClaudeProvider  # noqa: PLC0415

    providers_cfg = config.providers
    name = providers_cfg.default
    definition = providers_cfg.definitions.get(name)
    if definition is None:
        return ClaudeProvider()
    return build_provider(name, definition, config.models)


def call_claude(system: str, user: str, *, provider: "Provider | None" = None) -> str:
    """Run the configured provider in one-shot mode and return the text response.

    Builds the subprocess argv via ``provider.oneshot_command()`` so that
    brain planning honours the coordinator's configured backend rather than
    hard-coding ``claude``.

    When *provider* is ``None`` a :class:`~coord.providers.claude.ClaudeProvider`
    is used — matching the historical behaviour (``claude -p``).  Callers
    that want to honour the coordinator's configured backend should pass
    the result of :func:`_resolve_default_provider`.

    Args:
        system: The system prompt for the brain's planning call.
        user: The user message (piped to the subprocess via stdin).
        provider: Provider whose ``oneshot_command()`` is called to build
            the argv.  ``None`` falls back to
            :class:`~coord.providers.claude.ClaudeProvider`.

    Returns:
        The text response string.

    Raises:
        RuntimeError: When the subprocess exits with a non-zero return code.
    """
    if provider is None:
        from coord.providers.claude import ClaudeProvider  # noqa: PLC0415
        provider = ClaudeProvider()

    cmd = provider.oneshot_command(system_prompt=system, output_format="json")
    result = subprocess.run(
        cmd,
        input=user,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"brain provider call failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    # Try to extract the "result" field from a claude -p JSON envelope.
    # Falls back to raw stdout for providers that don't emit this shape
    # (e.g. OpenCodeProvider, whose output is unstructured).
    try:
        outer = json.loads(result.stdout)
        if isinstance(outer, dict) and "result" in outer:
            return outer["result"]
    except (json.JSONDecodeError, ValueError):
        pass
    return result.stdout


def _strip_fences(text: str) -> str:
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*\n(.*?)```\s*$", cleaned, re.DOTALL)
    return fence.group(1).strip() if fence else cleaned


def parse_proposals(text: str) -> list[Proposal]:
    """Parse the JSON response from Claude into Proposal objects."""
    data = json.loads(_strip_fences(text))
    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array of proposals, got {type(data).__name__}")

    proposals = []
    for i, item in enumerate(data):
        if item.get("type") == "split":
            continue
        proposals.append(Proposal(
            id=i + 1,
            machine_name=item["machine_name"],
            repo_name=item["repo_name"],
            issue_number=item["issue_number"],
            issue_title=item["issue_title"],
            rationale=item.get("rationale", ""),
            files_likely=item.get("files_likely", []),
            briefing=item.get("briefing", ""),
        ))
    return proposals


def resolve_required_gates(
    proposals: list[Proposal],
    config: Config,
    issues_by_repo: dict[str, list[dict]],
) -> None:
    """Resolve required_gates for each proposal from config.pipeline.labels.

    Mutates proposals in place: for each proposal, looks up the issue in
    ``issues_by_repo`` to get its GitHub labels, then checks each label against
    ``config.pipeline.labels``.  The first matching label wins.  If no label
    matches, ``required_gates`` is left unchanged (empty → caller falls back to
    ``config.pipeline.default_gates`` at dispatch/pipeline-view time).
    """
    if not config.pipeline.labels:
        return  # no label overrides configured, nothing to do

    for proposal in proposals:
        repo_issues = issues_by_repo.get(proposal.repo_name, [])
        issue = next(
            (iss for iss in repo_issues if iss.get("number") == proposal.issue_number),
            None,
        )
        if issue is None:
            continue
        issue_labels: list[str] = [
            lbl.get("name", "") for lbl in (issue.get("labels") or [])
        ]
        for lbl in issue_labels:
            if lbl in config.pipeline.labels:
                proposal.required_gates = list(config.pipeline.labels[lbl])
                break


def parse_split_proposals(text: str) -> list[SplitProposal]:
    """Parse split proposals from the brain's JSON response."""
    data = json.loads(_strip_fences(text))
    if not isinstance(data, list):
        return []

    splits = []
    for i, item in enumerate(data):
        if item.get("type") != "split":
            continue
        chunks = [
            SplitChunk(
                title=c["title"],
                scope=c.get("scope", ""),
                files_likely=c.get("files_likely", []),
            )
            for c in item.get("chunks", [])
        ]
        splits.append(SplitProposal(
            id=i + 1,
            repo_name=item["repo_name"],
            issue_number=item["issue_number"],
            issue_title=item["issue_title"],
            rationale=item.get("rationale", ""),
            chunks=chunks,
        ))
    return splits


def _annotate_large_proposals(proposals: list[Proposal], config: Config) -> None:
    """Append a split-suggestion note to proposals that exceed the file threshold.

    Mutates proposals in place.  The note is appended to ``rationale`` so it
    surfaces in ``coord plan`` output without cluttering the briefing itself.
    """
    threshold = config.dispatch.max_files_per_worker
    for p in proposals:
        if len(p.files_likely) > threshold:
            note = (
                f" [⚠ {len(p.files_likely)} files > threshold {threshold} — "
                "consider splitting via coord split]"
            )
            if note not in p.rationale:
                p.rationale += note


def _apply_require_plan(proposals: list[Proposal], config: Config) -> None:
    """When dispatch.require_plan is true, upgrade all work proposals to plan type.

    Mutates proposals in place.  Only work proposals are affected — review, smoke,
    and already-typed plan proposals are left unchanged.
    """
    if not config.dispatch.require_plan:
        return
    for p in proposals:
        if p.type == "work":
            p.type = "plan"


def propose(config: Config) -> tuple[list[Proposal], list[SplitProposal]]:
    """Full brain cycle: gather context, call the provider, return proposals and splits."""
    provider = _resolve_default_provider(config)
    context = gather_context(config)
    prompt = build_prompt(config, context)
    response = call_claude(SYSTEM_PROMPT, prompt, provider=provider)
    proposals = parse_proposals(response)
    _apply_require_plan(proposals, config)
    resolve_required_gates(proposals, config, context["issues_by_repo"])
    _annotate_large_proposals(proposals, config)
    return proposals, parse_split_proposals(response)
