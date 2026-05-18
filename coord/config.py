"""Parse and validate coordinator.yml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from coord.models import Machine, Repo


DEFAULT_CONFIG_PATH = Path("coordinator.yml")


class ConfigError(Exception):
    """Raised when coordinator.yml is missing, malformed, or fails validation."""


@dataclass
class Config:
    repos: list[Repo]
    machines: list[Machine]
    path: Path | None = None

    def repo(self, name: str) -> Repo | None:
        return next((r for r in self.repos if r.name == name), None)


def load(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    """Load and validate a coordinator.yml file."""
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config file not found: {p}")

    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {p}: {e}") from e

    if raw is None:
        raise ConfigError(f"Config file is empty: {p}")
    if not isinstance(raw, dict):
        raise ConfigError(f"Top-level config must be a mapping, got {type(raw).__name__}")

    repos = _parse_repos(raw.get("repos"))
    machines = _parse_machines(raw.get("machines"), repos)
    _validate_dependencies(repos)

    return Config(repos=repos, machines=machines, path=p)


def _parse_repos(raw: Any) -> list[Repo]:
    if raw is None:
        raise ConfigError("Config must define 'repos'")
    if not isinstance(raw, list):
        raise ConfigError("'repos' must be a list")
    if not raw:
        raise ConfigError("'repos' must contain at least one repo")

    repos: list[Repo] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"repos[{i}] must be a mapping, got {type(entry).__name__}")
        name = entry.get("name")
        github = entry.get("github")
        if not name or not isinstance(name, str):
            raise ConfigError(f"repos[{i}].name is required (string)")
        if not github or not isinstance(github, str):
            raise ConfigError(f"repos[{i}].github is required (string, 'owner/repo')")
        if "/" not in github:
            raise ConfigError(
                f"repos[{i}].github must be 'owner/repo', got {github!r}"
            )
        if name in seen:
            raise ConfigError(f"duplicate repo name: {name!r}")
        seen.add(name)

        depends_on = entry.get("depends_on", []) or []
        if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
            raise ConfigError(f"repos[{i}].depends_on must be a list of repo names")

        default_branch = entry.get("default_branch", "main")
        if not isinstance(default_branch, str):
            raise ConfigError(f"repos[{i}].default_branch must be a string")

        repos.append(
            Repo(
                name=name,
                github=github,
                depends_on=depends_on,
                default_branch=default_branch,
            )
        )
    return repos


def _parse_machines(raw: Any, repos: list[Repo]) -> list[Machine]:
    if raw is None:
        raise ConfigError("Config must define 'machines'")
    if not isinstance(raw, list):
        raise ConfigError("'machines' must be a list")
    if not raw:
        raise ConfigError("'machines' must contain at least one machine")

    repo_names = {r.name for r in repos}
    machines: list[Machine] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"machines[{i}] must be a mapping, got {type(entry).__name__}")
        name = entry.get("name")
        host = entry.get("host")
        if not name or not isinstance(name, str):
            raise ConfigError(f"machines[{i}].name is required (string)")
        if not host or not isinstance(host, str):
            raise ConfigError(f"machines[{i}].host is required (string, tailscale hostname)")
        if name in seen:
            raise ConfigError(f"duplicate machine name: {name!r}")
        seen.add(name)

        capabilities = entry.get("capabilities", []) or []
        if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
            raise ConfigError(f"machines[{i}].capabilities must be a list of strings")

        machine_repos = entry.get("repos", []) or []
        if not isinstance(machine_repos, list) or not all(isinstance(r, str) for r in machine_repos):
            raise ConfigError(f"machines[{i}].repos must be a list of repo names")

        unknown = [r for r in machine_repos if r not in repo_names]
        if unknown:
            raise ConfigError(
                f"machines[{i}] ({name!r}) references unknown repos: {unknown}"
            )

        machines.append(
            Machine(
                name=name,
                host=host,
                capabilities=capabilities,
                repos=machine_repos,
            )
        )
    return machines


def _validate_dependencies(repos: list[Repo]) -> None:
    repo_names = {r.name for r in repos}
    for r in repos:
        unknown = [d for d in r.depends_on if d not in repo_names]
        if unknown:
            raise ConfigError(
                f"repo {r.name!r} depends_on unknown repos: {unknown}"
            )
        if r.name in r.depends_on:
            raise ConfigError(f"repo {r.name!r} cannot depend on itself")
