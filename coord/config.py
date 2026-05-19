"""Parse and validate coordinator.yml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from coord.models import Machine, Repo


DEFAULT_CONFIG_PATH = Path("coordinator.yml")


class ConfigError(Exception):
    """Raised when coordinator.yml is missing, malformed, or fails validation."""


@dataclass
class HooksConfig:
    on_round_complete: list[str] = field(default_factory=list)
    on_session_end: list[str] = field(default_factory=list)


@dataclass
class ReviewsConfig:
    """Adversarial code review settings.

    `enabled=False` by default — opt-in per project. When enabled, completion
    of a "work" assignment triggers an automatic review dispatch to a
    different machine (see coord/review.py).
    """

    enabled: bool = False
    auto_dispatch: bool = True
    require_approval: bool = False
    reviewer_prompt: str = ""
    checklist: list[str] = field(default_factory=list)
    repo_overrides: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class SmokeTestsConfig:
    enabled: bool = False
    auto_dispatch: bool = True
    timeout: int = 600


@dataclass
class ConcurrencyConfig:
    max_workers: int = 2
    stagger_seconds: float = 30.0
    backoff_base: float = 60.0
    max_retries: int = 3


@dataclass
class Config:
    repos: list[Repo]
    machines: list[Machine]
    hooks: HooksConfig = field(default_factory=HooksConfig)
    reviews: ReviewsConfig = field(default_factory=ReviewsConfig)
    smoke_tests: SmokeTestsConfig = field(default_factory=SmokeTestsConfig)
    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)
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
    hooks = _parse_hooks(raw.get("hooks"))
    reviews = _parse_reviews(raw.get("reviews"), {r.name for r in repos})
    smoke_tests = _parse_smoke_tests(raw.get("smoke_tests"))
    concurrency = _parse_concurrency(raw.get("concurrency"))

    return Config(repos=repos, machines=machines, hooks=hooks, reviews=reviews, smoke_tests=smoke_tests, concurrency=concurrency, path=p)


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

        build_command = entry.get("build_command")
        if build_command is not None and not isinstance(build_command, str):
            raise ConfigError(f"repos[{i}].build_command must be a string")
        test_command = entry.get("test_command")
        if test_command is not None and not isinstance(test_command, str):
            raise ConfigError(f"repos[{i}].test_command must be a string")

        repos.append(
            Repo(
                name=name,
                github=github,
                depends_on=depends_on,
                default_branch=default_branch,
                build_command=build_command,
                test_command=test_command,
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

        repo_paths = entry.get("repo_paths", {}) or {}
        if not isinstance(repo_paths, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in repo_paths.items()
        ):
            raise ConfigError(f"machines[{i}].repo_paths must be a mapping of repo name → local path")
        unknown_paths = [r for r in repo_paths if r not in repo_names]
        if unknown_paths:
            raise ConfigError(
                f"machines[{i}] ({name!r}) repo_paths references unknown repos: {unknown_paths}"
            )

        machines.append(
            Machine(
                name=name,
                host=host,
                capabilities=capabilities,
                repos=machine_repos,
                repo_paths=repo_paths,
            )
        )
    return machines


KNOWN_HOOKS = {"close_merged_issues", "summary_report"}


def _parse_hooks(raw: Any) -> HooksConfig:
    if raw is None:
        return HooksConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'hooks' must be a mapping")
    hooks = HooksConfig()
    for event_name in ("on_round_complete", "on_session_end"):
        entries = raw.get(event_name)
        if entries is None:
            continue
        if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
            raise ConfigError(f"hooks.{event_name} must be a list of hook names")
        unknown = [e for e in entries if e not in KNOWN_HOOKS]
        if unknown:
            raise ConfigError(
                f"hooks.{event_name} references unknown hooks: {unknown}. "
                f"Known: {sorted(KNOWN_HOOKS)}"
            )
        setattr(hooks, event_name, entries)
    return hooks


def _parse_reviews(raw: Any, repo_names: set[str]) -> ReviewsConfig:
    if raw is None:
        return ReviewsConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'reviews' must be a mapping")

    cfg = ReviewsConfig()
    for bool_field in ("enabled", "auto_dispatch", "require_approval"):
        if bool_field in raw:
            value = raw[bool_field]
            if not isinstance(value, bool):
                raise ConfigError(f"reviews.{bool_field} must be a boolean")
            setattr(cfg, bool_field, value)

    if "reviewer_prompt" in raw:
        value = raw["reviewer_prompt"]
        if not isinstance(value, str):
            raise ConfigError("reviews.reviewer_prompt must be a string")
        cfg.reviewer_prompt = value

    checklist = raw.get("checklist", []) or []
    if not isinstance(checklist, list) or not all(isinstance(c, str) for c in checklist):
        raise ConfigError("reviews.checklist must be a list of strings")
    cfg.checklist = checklist

    overrides = raw.get("repo_overrides", {}) or {}
    if not isinstance(overrides, dict):
        raise ConfigError("reviews.repo_overrides must be a mapping of repo → list of strings")
    for repo_name, items in overrides.items():
        if not isinstance(repo_name, str):
            raise ConfigError("reviews.repo_overrides keys must be repo names")
        if repo_name not in repo_names:
            raise ConfigError(
                f"reviews.repo_overrides references unknown repo: {repo_name!r}"
            )
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            raise ConfigError(
                f"reviews.repo_overrides[{repo_name}] must be a list of strings"
            )
    cfg.repo_overrides = overrides
    return cfg


def _parse_smoke_tests(raw: Any) -> SmokeTestsConfig:
    if raw is None:
        return SmokeTestsConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'smoke_tests' must be a mapping")
    cfg = SmokeTestsConfig()
    for bool_field in ("enabled", "auto_dispatch"):
        if bool_field in raw:
            value = raw[bool_field]
            if not isinstance(value, bool):
                raise ConfigError(f"smoke_tests.{bool_field} must be a boolean")
            setattr(cfg, bool_field, value)
    if "timeout" in raw:
        value = raw["timeout"]
        if not isinstance(value, int) or value < 0:
            raise ConfigError("smoke_tests.timeout must be a non-negative integer")
        cfg.timeout = value
    return cfg


def _parse_concurrency(raw: Any) -> ConcurrencyConfig:
    if raw is None:
        return ConcurrencyConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'concurrency' must be a mapping")
    cfg = ConcurrencyConfig()
    for key in ("max_workers", "stagger_seconds", "backoff_base", "max_retries"):
        val = raw.get(key)
        if val is None:
            continue
        if key == "max_retries" or key == "max_workers":
            if not isinstance(val, int) or val < 0:
                raise ConfigError(f"concurrency.{key} must be a non-negative integer")
        else:
            if not isinstance(val, (int, float)) or val < 0:
                raise ConfigError(f"concurrency.{key} must be a non-negative number")
        setattr(cfg, key, val)
    return cfg


def _validate_dependencies(repos: list[Repo]) -> None:
    from coord.deps import detect_cycles

    repo_names = {r.name for r in repos}
    for r in repos:
        unknown = [d for d in r.depends_on if d not in repo_names]
        if unknown:
            raise ConfigError(
                f"repo {r.name!r} depends_on unknown repos: {unknown}"
            )
        if r.name in r.depends_on:
            raise ConfigError(f"repo {r.name!r} cannot depend on itself")

    cycles = detect_cycles(repos)
    if cycles:
        cycle_str = " → ".join(cycles[0])
        raise ConfigError(f"circular dependency detected: {cycle_str}")
