"""Tests for coord.deps — dependency graph, cycle detection, blocked repos."""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.config import ConfigError, load
from coord.deps import (
    blocked_repos,
    build_dep_graph,
    dependents,
    detect_cycles,
    topo_sort,
    transitive_deps,
)
from coord.models import Assignment, Repo


# ── Graph building ─────────────────────────────────────────────────────────


class TestBuildDepGraph:
    def test_simple_graph(self) -> None:
        repos = [
            Repo(name="api", github="a/a", depends_on=["shared"]),
            Repo(name="shared", github="a/s"),
        ]
        graph = build_dep_graph(repos)
        assert graph == {"api": ["shared"], "shared": []}

    def test_no_deps(self) -> None:
        repos = [Repo(name="api", github="a/a")]
        graph = build_dep_graph(repos)
        assert graph == {"api": []}

    def test_multi_deps(self) -> None:
        repos = [
            Repo(name="frontend", github="a/f", depends_on=["api", "shared"]),
            Repo(name="api", github="a/a", depends_on=["shared"]),
            Repo(name="shared", github="a/s"),
        ]
        graph = build_dep_graph(repos)
        assert set(graph["frontend"]) == {"api", "shared"}
        assert graph["api"] == ["shared"]


# ── Transitive deps ───────────────────────────────────────────────────────


class TestTransitiveDeps:
    def test_direct_dep(self) -> None:
        graph = {"api": ["shared"], "shared": []}
        assert transitive_deps("api", graph) == {"shared"}

    def test_transitive_chain(self) -> None:
        graph = {"frontend": ["api"], "api": ["shared"], "shared": []}
        assert transitive_deps("frontend", graph) == {"api", "shared"}

    def test_no_deps(self) -> None:
        graph = {"shared": []}
        assert transitive_deps("shared", graph) == set()

    def test_diamond(self) -> None:
        graph = {
            "app": ["api", "config"],
            "api": ["shared"],
            "config": ["shared"],
            "shared": [],
        }
        assert transitive_deps("app", graph) == {"api", "config", "shared"}


# ── Dependents ─────────────────────────────────────────────────────────────


class TestDependents:
    def test_direct_dependent(self) -> None:
        graph = {"api": ["shared"], "shared": []}
        assert dependents("shared", graph) == {"api"}

    def test_transitive_dependents(self) -> None:
        graph = {"frontend": ["api"], "api": ["shared"], "shared": []}
        assert dependents("shared", graph) == {"frontend", "api"}

    def test_no_dependents(self) -> None:
        graph = {"api": ["shared"], "shared": []}
        assert dependents("api", graph) == set()


# ── Cycle detection ────────────────────────────────────────────────────────


class TestDetectCycles:
    def test_no_cycles(self) -> None:
        repos = [
            Repo(name="api", github="a/a", depends_on=["shared"]),
            Repo(name="shared", github="a/s"),
        ]
        assert detect_cycles(repos) == []

    def test_direct_cycle(self) -> None:
        repos = [
            Repo(name="a", github="x/a", depends_on=["b"]),
            Repo(name="b", github="x/b", depends_on=["a"]),
        ]
        cycles = detect_cycles(repos)
        assert len(cycles) > 0
        flat = set()
        for c in cycles:
            flat.update(c)
        assert "a" in flat and "b" in flat

    def test_transitive_cycle(self) -> None:
        repos = [
            Repo(name="a", github="x/a", depends_on=["b"]),
            Repo(name="b", github="x/b", depends_on=["c"]),
            Repo(name="c", github="x/c", depends_on=["a"]),
        ]
        cycles = detect_cycles(repos)
        assert len(cycles) > 0

    def test_config_rejects_cycle(self, tmp_path: Path) -> None:
        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n"
            "  - name: a\n    github: x/a\n    depends_on: [b]\n"
            "  - name: b\n    github: x/b\n    depends_on: [a]\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [a, b]\n"
        )
        with pytest.raises(ConfigError, match="circular dependency"):
            load(config_file)


# ── Topo sort ──────────────────────────────────────────────────────────────


class TestTopoSort:
    def test_simple_chain(self) -> None:
        repos = [
            Repo(name="api", github="a/a", depends_on=["shared"]),
            Repo(name="shared", github="a/s"),
        ]
        order = topo_sort(repos)
        assert order.index("shared") < order.index("api")

    def test_no_deps(self) -> None:
        repos = [
            Repo(name="a", github="x/a"),
            Repo(name="b", github="x/b"),
        ]
        order = topo_sort(repos)
        assert set(order) == {"a", "b"}

    def test_diamond(self) -> None:
        repos = [
            Repo(name="app", github="x/app", depends_on=["api", "config"]),
            Repo(name="api", github="x/api", depends_on=["shared"]),
            Repo(name="config", github="x/cfg", depends_on=["shared"]),
            Repo(name="shared", github="x/s"),
        ]
        order = topo_sort(repos)
        assert order.index("shared") < order.index("api")
        assert order.index("shared") < order.index("config")
        assert order.index("api") < order.index("app")
        assert order.index("config") < order.index("app")


# ── Blocked repos ──────────────────────────────────────────────────────────


class TestBlockedRepos:
    def test_no_active_means_nothing_blocked(self) -> None:
        repos = [
            Repo(name="api", github="a/a", depends_on=["shared"]),
            Repo(name="shared", github="a/s"),
        ]
        assert blocked_repos(repos, []) == {}

    def test_upstream_active_blocks_downstream(self) -> None:
        repos = [
            Repo(name="api", github="a/a", depends_on=["shared"]),
            Repo(name="shared", github="a/s"),
        ]
        active = [
            Assignment(
                machine_name="laptop", repo_name="shared",
                issue_number=42, issue_title="Refactor API",
                status="running",
            ),
        ]
        blocked = blocked_repos(repos, active)
        assert "api" in blocked
        assert "shared" not in blocked
        assert "shared #42" in blocked["api"][0]

    def test_transitive_blocking(self) -> None:
        repos = [
            Repo(name="frontend", github="a/f", depends_on=["api"]),
            Repo(name="api", github="a/a", depends_on=["shared"]),
            Repo(name="shared", github="a/s"),
        ]
        active = [
            Assignment(
                machine_name="server", repo_name="shared",
                issue_number=10, issue_title="Update types",
                status="running",
            ),
        ]
        blocked = blocked_repos(repos, active)
        assert "api" in blocked
        assert "frontend" in blocked
        assert "shared" not in blocked

    def test_non_running_assignments_ignored(self) -> None:
        repos = [
            Repo(name="api", github="a/a", depends_on=["shared"]),
            Repo(name="shared", github="a/s"),
        ]
        active = [
            Assignment(
                machine_name="laptop", repo_name="shared",
                issue_number=1, issue_title="Done",
                status="done",
            ),
        ]
        assert blocked_repos(repos, active) == {}

    def test_independent_repos_not_blocked(self) -> None:
        repos = [
            Repo(name="api", github="a/a"),
            Repo(name="web", github="a/w"),
        ]
        active = [
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=1, issue_title="X",
                status="running",
            ),
        ]
        assert blocked_repos(repos, active) == {}


# ── Brain prompt includes blocked repos ────────────────────────────────────


class TestBrainPromptDeps:
    def test_blocked_section_appears_when_upstream_active(self) -> None:
        from coord.brain import build_prompt
        from coord.config import Config
        from coord.models import Machine

        config = Config(
            repos=[
                Repo(name="api", github="a/a", depends_on=["shared"]),
                Repo(name="shared", github="a/s"),
            ],
            machines=[
                Machine(name="laptop", host="laptop.tailnet", repos=["api", "shared"]),
            ],
        )
        context = {
            "issues_by_repo": {"api": [], "shared": []},
            "machine_status": {
                "laptop": {
                    "active": [{
                        "id": "x",
                        "status": "running",
                        "spec": {
                            "repo_name": "shared",
                            "issue_number": 42,
                            "issue_title": "Refactor",
                        },
                    }],
                    "completed": [],
                },
            },
        }
        prompt = build_prompt(config, context)
        assert "BLOCKED" in prompt
        assert "api" in prompt
        assert "shared #42" in prompt

    def test_no_blocked_section_when_no_deps(self) -> None:
        from coord.brain import build_prompt
        from coord.config import Config
        from coord.models import Machine

        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[Machine(name="laptop", host="h")],
        )
        context = {
            "issues_by_repo": {"api": []},
            "machine_status": {"laptop": {"status": "idle"}},
        }
        prompt = build_prompt(config, context)
        assert "BLOCKED" not in prompt
