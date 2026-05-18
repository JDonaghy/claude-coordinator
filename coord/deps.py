"""Dependency graph utilities for multi-repo constraint enforcement."""

from __future__ import annotations

from coord.models import Assignment, Repo


def build_dep_graph(repos: list[Repo]) -> dict[str, list[str]]:
    """Map each repo to the repos it directly depends on."""
    return {r.name: list(r.depends_on) for r in repos}


def transitive_deps(repo_name: str, graph: dict[str, list[str]]) -> set[str]:
    """All repos that *repo_name* transitively depends on."""
    visited: set[str] = set()
    stack = list(graph.get(repo_name, []))
    while stack:
        dep = stack.pop()
        if dep in visited:
            continue
        visited.add(dep)
        stack.extend(graph.get(dep, []))
    return visited


def dependents(repo_name: str, graph: dict[str, list[str]]) -> set[str]:
    """All repos that directly or transitively depend on *repo_name*."""
    result: set[str] = set()
    for name in graph:
        if repo_name in transitive_deps(name, graph):
            result.add(name)
    return result


def detect_cycles(repos: list[Repo]) -> list[list[str]]:
    """Return a list of cycles found in the dependency graph.

    Each cycle is a list of repo names forming the loop. Returns []
    if the graph is a valid DAG.
    """
    graph = build_dep_graph(repos)
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {name: WHITE for name in graph}
    cycles: list[list[str]] = []

    def dfs(node: str, path: list[str]) -> None:
        color[node] = GRAY
        path.append(node)
        for dep in graph.get(node, []):
            if dep not in color:
                continue
            if color[dep] == GRAY:
                cycle_start = path.index(dep)
                cycles.append(path[cycle_start:] + [dep])
            elif color[dep] == WHITE:
                dfs(dep, path)
        path.pop()
        color[node] = BLACK

    for node in graph:
        if color[node] == WHITE:
            dfs(node, [])
    return cycles


def topo_sort(repos: list[Repo]) -> list[str]:
    """Topological sort of repo names. Dependencies come before dependents."""
    graph = build_dep_graph(repos)

    # in_degree[X] = number of repos X depends on (i.e., must come before X)
    in_degree: dict[str, int] = {name: len(deps) for name, deps in graph.items()}

    result: list[str] = []
    queue = sorted(n for n, deg in in_degree.items() if deg == 0)
    while queue:
        node = queue.pop(0)
        result.append(node)
        # For every repo that depends on `node`, decrement its in-degree
        for name, deps in graph.items():
            if node in deps:
                in_degree[name] -= 1
                if in_degree[name] == 0:
                    queue.append(name)

    return result


def blocked_repos(
    repos: list[Repo],
    active: list[Assignment],
) -> dict[str, list[str]]:
    """Determine which repos are blocked by active upstream work.

    Returns a map of blocked_repo_name → list of human-readable reasons.
    A repo is blocked if any repo it (transitively) depends on has a
    running assignment.
    """
    graph = build_dep_graph(repos)
    active_by_repo: dict[str, list[Assignment]] = {}
    for a in active:
        if a.status == "running":
            active_by_repo.setdefault(a.repo_name, []).append(a)

    blocked: dict[str, list[str]] = {}
    for repo in repos:
        reasons: list[str] = []
        for dep in transitive_deps(repo.name, graph):
            dep_assignments = active_by_repo.get(dep, [])
            for a in dep_assignments:
                reasons.append(
                    f"{dep} #{a.issue_number} ({a.issue_title}) is in progress on {a.machine_name}"
                )
        if reasons:
            blocked[repo.name] = reasons
    return blocked
