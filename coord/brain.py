"""Coordinator brain — gathers context and calls claude -p to propose assignments."""

from __future__ import annotations

import json
import re
import subprocess
import httpx

from coord.config import Config
from coord.models import Proposal, SplitChunk, SplitProposal
from coord import github_ops

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
    issues_by_repo: dict[str, list[dict]] = {}
    for repo in config.repos:
        try:
            issues_by_repo[repo.name] = github_ops.get_open_issues(repo.github)
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
    lines.append("## Machines")
    for machine in config.machines:
        caps = ", ".join(machine.capabilities) if machine.capabilities else "none"
        repos = ", ".join(machine.repos) if machine.repos else "none"
        status = context["machine_status"].get(machine.name, {})
        if status.get("status") == "offline":
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
                preview = body[:300]
                if len(body) > 300:
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


def call_claude(system: str, user: str) -> str:
    """Run claude -p and return the text response."""
    result = subprocess.run(
        ["claude", "-p", "--system-prompt", system, "--output-format", "json"],
        input=user,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude -p failed (exit {result.returncode}): {result.stderr.strip()}")

    outer = json.loads(result.stdout)
    return outer.get("result", result.stdout)


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


def propose(config: Config) -> tuple[list[Proposal], list[SplitProposal]]:
    """Full brain cycle: gather context, call Claude, return proposals and splits."""
    context = gather_context(config)
    prompt = build_prompt(config, context)
    response = call_claude(SYSTEM_PROMPT, prompt)
    return parse_proposals(response), parse_split_proposals(response)
