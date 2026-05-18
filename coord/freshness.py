"""Compare agent-reported local repo state against GitHub HEADs.

Pure logic — no IO. Callers pass in the data they fetched from agents and
GitHub; this module decides what's stale, current, dirty, or unknown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from coord.config import Config
from coord.deps import build_dep_graph, transitive_deps
from coord.models import Proposal

CURRENT = "current"
STALE = "stale"
DIRTY = "dirty"
MISSING = "missing"
UNKNOWN = "unknown"


@dataclass
class RepoFreshness:
    repo_name: str
    state: str  # one of the constants above
    local_sha: str | None = None
    remote_sha: str | None = None
    branch: str | None = None
    dirty: bool = False
    error: str | None = None

    @property
    def needs_pull(self) -> bool:
        return self.state == STALE


def compare(
    repo_name: str,
    local_info: dict | None,
    remote_sha: str | None,
) -> RepoFreshness:
    """Classify one repo given its agent-reported local info and the remote SHA.

    `local_info` is the dict returned by `AgentServer.list_repos()[repo_name]`
    (or None if the agent had no entry). `remote_sha` is the full SHA on
    GitHub's default branch (or None if lookup failed).
    """
    if local_info is None:
        return RepoFreshness(repo_name=repo_name, state=MISSING, remote_sha=remote_sha,
                             error="agent did not report this repo")

    error = local_info.get("error")
    if error:
        return RepoFreshness(
            repo_name=repo_name,
            state=MISSING,
            remote_sha=remote_sha,
            error=error,
        )

    local_sha = local_info.get("sha")
    branch = local_info.get("branch")
    dirty = bool(local_info.get("dirty"))

    if remote_sha is None:
        return RepoFreshness(
            repo_name=repo_name,
            state=UNKNOWN,
            local_sha=local_sha,
            branch=branch,
            dirty=dirty,
            error="remote head not available",
        )

    if dirty:
        return RepoFreshness(
            repo_name=repo_name,
            state=DIRTY,
            local_sha=local_sha,
            remote_sha=remote_sha,
            branch=branch,
            dirty=True,
        )

    state = CURRENT if local_sha == remote_sha else STALE
    return RepoFreshness(
        repo_name=repo_name,
        state=state,
        local_sha=local_sha,
        remote_sha=remote_sha,
        branch=branch,
    )


def dependency_freshness(
    proposal: Proposal,
    config: Config,
    repo_heads: dict[str, dict],
    github_heads: dict[str, str | None],
) -> list[RepoFreshness]:
    """Freshness for every transitive dep of proposal.repo_name.

    `repo_heads` is what the target agent returned from /repos.
    `github_heads` maps repo_name -> remote SHA (None if lookup failed).
    """
    graph = build_dep_graph(config.repos)
    dep_names = transitive_deps(proposal.repo_name, graph)
    return [
        compare(dep, repo_heads.get(dep), github_heads.get(dep))
        for dep in sorted(dep_names)
    ]


def stale_or_dirty(freshness: Iterable[RepoFreshness]) -> list[RepoFreshness]:
    """Filter to entries that need user attention."""
    return [f for f in freshness if f.state in (STALE, DIRTY, MISSING)]


def format_briefing_addendum(freshness: list[RepoFreshness]) -> str:
    """Markdown block to append to a worker's briefing when deps are stale."""
    needs = stale_or_dirty(freshness)
    if not needs:
        return ""
    lines = ["", "### Stale dependencies", "Before building, pull these dependency repos:"]
    for f in needs:
        if f.state == STALE:
            lines.append(
                f"- `{f.repo_name}` (local {f.local_sha[:7] if f.local_sha else '?'} "
                f"→ remote {f.remote_sha[:7] if f.remote_sha else '?'})"
            )
        elif f.state == DIRTY:
            lines.append(
                f"- `{f.repo_name}` is **dirty** on the agent — uncommitted changes; "
                f"do not pull, surface to operator"
            )
        else:
            lines.append(f"- `{f.repo_name}`: {f.error or 'unknown state'}")
    return "\n".join(lines)
