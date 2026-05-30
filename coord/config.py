"""Parse and validate coordinator.yml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from coord.models import Machine, Repo, WorkerPermissionsConfig


DEFAULT_CONFIG_PATH = Path("coordinator.yml")

# Safety-by-default: repos without explicit worker_permissions get this deny-list.
DEFAULT_DENY_COMMANDS: list[str] = [
    "Bash(gh *)",
    "Bash(git push --force *)",
    "Bash(git push -f *)",
    "Bash(git reset --hard *)",
    "Bash(git branch -D *)",
    "Bash(git checkout -- .)",
    "Bash(git clean -f *)",
    "Bash(rm -rf *)",
]


class ConfigError(Exception):
    """Raised when coordinator.yml is missing, malformed, or fails validation."""


@dataclass
class HooksConfig:
    on_round_complete: list[str] = field(default_factory=list)
    on_session_end: list[str] = field(default_factory=list)


@dataclass
class ReviewsConfig:
    """Adversarial code review settings.

    `enabled=True` by default. When enabled, `coord pr` auto-dispatches an
    adversarial review to a different machine after the PR worker is sent.
    Completion of a "work" assignment via reconciliation also triggers review
    dispatch automatically (see coord/review.py). Set `enabled: false` in
    coordinator.yml to opt out.
    """

    enabled: bool = True
    auto_dispatch: bool = True
    require_approval: bool = False
    reviewer_prompt: str = ""
    checklist: list[str] = field(default_factory=lambda: [
        "Check for platform-specific code in shared/cross-platform paths",
    ])
    repo_overrides: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class ConcurrencyConfig:
    max_workers: int = 2
    stagger_seconds: float = 30.0
    backoff_base: float = 60.0
    max_retries: int = 3
    auto_reassign: bool = False
    stale_threshold: int = 3
    # Spawn `claude -p` through a transient `bash -c 'exec ...'` parent so the
    # immediate parent of claude is a short-lived shell. This is the upstream
    # headline fix for the daemon-spawn freeze (anthropics/claude-code#56268).
    bash_wrap_spawn: bool = True
    # First-output (TTFT) watchdog: if a worker produces zero output within
    # this many seconds, kill its process group and fail the assignment so the
    # auto_reassign path re-dispatches it. 0 disables the watchdog. This only
    # catches truly silent hangs — a rate-limited worker still emits output and
    # therefore passes the check.
    first_output_timeout: float = 600.0


@dataclass
class SmokeRule:
    """When a worker's diff touches any of `files`, the smoke machine must
    have all capabilities in `requires`.

    `files` patterns match by prefix against the relative paths returned by
    `gh pr view --json files`. A trailing `/` makes the prefix explicit; bare
    paths match if the touched path starts with the rule path (so `src/gtk`
    catches `src/gtk/foo.c` and `src/gtk_helpers.c`). Use `src/gtk/` to scope
    strictly to the directory.
    """

    files: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)


@dataclass
class SmokeTestsConfig:
    """Smoke-test orchestration. Off by default — opt-in per project.

    `default_command` is the shell command the smoke agent runs (e.g.
    `make smoke` or `pytest tests/smoke`). Per-repo overrides flow through
    `Repo.test_command` already; this is the fallback when none is set.
    """

    auto_queue: bool = False
    default_command: str | None = None
    timeout_seconds: int = 600
    capability_rules: list[SmokeRule] = field(default_factory=list)


@dataclass
class ModelsConfig:
    """Model tier selection and escalation ladder for workers.

    `default` is the model passed to ``claude -p`` when an assignment doesn't
    specify one.  `escalation` is an ordered list of model aliases (low →
    high); when a worker fails or gets stuck, the coordinator escalates to
    the next entry via `next_model`.  `labels` is a per-issue-label override
    (e.g. ``documentation: haiku``) consumed by the brain / planner.

    `versions` pins an alias to an exact model id, e.g.
    ``{sonnet: claude-sonnet-4-6, opus: claude-opus-4-7}``.  When set, the
    coordinator translates the alias to the exact id before passing it to
    ``claude -p --model`` on the worker.  Aliases not present in the map
    pass through unchanged, so ``claude -p`` falls back to its CLI default
    (which today is whatever the installed claude-cli treats as latest).
    """

    default: str = "sonnet"
    escalation: list[str] = field(
        default_factory=lambda: ["haiku", "sonnet", "opus"]
    )
    labels: dict[str, str] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)

    def next_model(self, current: str) -> str:
        """Return the next model in the escalation ladder.

        If *current* is already at the top of the ladder, or isn't on the
        ladder at all, return *current* unchanged.
        """
        try:
            idx = self.escalation.index(current)
        except ValueError:
            return current
        if idx + 1 < len(self.escalation):
            return self.escalation[idx + 1]
        return current

    def resolve(self, alias: str | None) -> str | None:
        """Resolve an alias to its pinned exact model id, if configured.

        Returns *alias* unchanged when no mapping exists, and ``None`` when
        *alias* is ``None`` (preserves the "omit --model" code path).
        """
        if alias is None:
            return None
        return self.versions.get(alias, alias)


@dataclass
class DispatchConfig:
    """Smart task-splitting configuration.

    When ``auto_split`` is ``True`` (the default), the ``coord approve``
    command analyses each proposal's ``files_likely`` list.  If the file
    count exceeds ``max_files_per_worker``, the work is shown to the user
    split into parallel/sequential chunks for confirmation before dispatch.

    Set ``auto_split: false`` to disable the splitting analysis entirely.

    When ``require_plan`` is ``True``, ``coord assign`` defaults to
    ``--plan-only`` behaviour — the worker reads the codebase and produces a
    structured plan without writing any code.  The user then runs
    ``coord approve-plan`` or ``coord reject-plan`` to act on the plan.
    Pass ``--no-plan`` to ``coord assign`` to override this default and
    dispatch a work assignment directly.  Assignments of type ``review``,
    ``smoke``, or ``plan`` are never affected by this setting.
    """

    max_files_per_worker: int = 8
    auto_split: bool = True
    require_plan: bool = False


@dataclass
class PipelineConfig:
    """Assignment lifecycle gate configuration.

    ``default_gates`` is the list of approval steps required for every work
    assignment unless overridden by an issue label.  ``labels`` maps GitHub
    issue label names to gate lists, allowing per-label overrides — e.g.
    a ``hotfix`` label could bypass review with ``hotfix: [merge]``.

    ``auto_loop`` enables the automated review → fix → re-review cycle.
    When ``True`` (default), a review that requests changes automatically
    dispatches a fix worker.  The fix worker then receives a fresh review,
    and the cycle continues until the review approves or
    ``max_review_iterations`` is reached.

    ``max_review_iterations`` is the maximum number of fix rounds before
    the auto-loop stops and posts a notice asking for manual intervention.
    Default is 3.

    ``escalate_fix_model`` controls whether auto-dispatched fix workers
    escalate the model on each bounce iteration.  When ``True`` (default),
    the first fix stays on ``models.default`` and each subsequent fix
    iteration climbs one rung up ``models.escalation`` (capped at the top).
    When ``False``, fix dispatches set no model (today's behaviour: the
    agent falls back to ``claude -p``'s default).
    """

    default_gates: list[str] = field(default_factory=lambda: ["test", "review", "merge"])
    labels: dict[str, list[str]] = field(default_factory=dict)
    auto_loop: bool = True
    max_review_iterations: int = 3
    escalate_fix_model: bool = True

    def tracked_labels(self) -> list[str]:
        """Return the GitHub issue labels considered part of the pipeline.

        Always includes ``'coord'`` so normal coordinator-tagged issues appear
        in the pipeline panel regardless of per-label gate configuration.
        Additional labels come from the ``labels`` dict keys, sorted for
        stable ordering.
        """
        if not self.labels:
            return ["coord"]
        keys = sorted(self.labels.keys())
        if "coord" not in keys:
            keys = ["coord"] + keys
        return keys

    def gates_for_label(self, label: str | None) -> list[str]:
        """Return the gate list for a specific label, falling back to defaults.

        ``label`` may be ``None`` (no matching tracked label found on the
        issue) — in that case the configured ``default_gates`` are returned.
        """
        if label and label in self.labels:
            return list(self.labels[label])
        return list(self.default_gates)


@dataclass
class CiStoreConfig:
    """Backend selection for CI check visibility (#240).

    ``type`` is one of ``github`` (shell out to ``gh pr checks``) or
    ``none`` (always-empty :class:`coord.ci_store.NoOpCi`).  When the block
    is absent we default to ``github`` since it's a no-op upgrade for users
    who already have ``gh`` configured.  Future backends (GitLab, Buildkite)
    add new ``type`` values without breaking existing configs.
    """

    type: str = "github"


@dataclass
class Config:
    repos: list[Repo]
    machines: list[Machine]
    hooks: HooksConfig = field(default_factory=HooksConfig)
    reviews: ReviewsConfig = field(default_factory=ReviewsConfig)
    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)
    smoke_tests: SmokeTestsConfig = field(default_factory=SmokeTestsConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    dispatch: DispatchConfig = field(default_factory=DispatchConfig)
    ci_store: CiStoreConfig = field(default_factory=CiStoreConfig)
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
    concurrency = _parse_concurrency(raw.get("concurrency"))
    smoke_tests = _parse_smoke_tests(raw.get("smoke_tests"))
    models = _parse_models(raw.get("models"))
    pipeline = _parse_pipeline(raw.get("pipeline"))
    dispatch = _parse_dispatch(raw.get("dispatch"))
    ci_store = _parse_ci_store(raw.get("ci_store"))

    return Config(
        repos=repos,
        machines=machines,
        hooks=hooks,
        reviews=reviews,
        concurrency=concurrency,
        smoke_tests=smoke_tests,
        models=models,
        pipeline=pipeline,
        dispatch=dispatch,
        ci_store=ci_store,
        path=p,
    )


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
        # #296: run_cmd — optional shell command to launch the app for manual
        # smoke testing.  Surfaced in the TUI Test stage detail panel.
        run_cmd = entry.get("run_cmd")
        if run_cmd is not None and not isinstance(run_cmd, str):
            raise ConfigError(f"repos[{i}].run_cmd must be a string")

        worker_permissions = _parse_worker_permissions(entry.get("worker_permissions"), i)

        housekeeping = entry.get("housekeeping", []) or []
        if not isinstance(housekeeping, list) or not all(isinstance(h, str) for h in housekeeping):
            raise ConfigError(f"repos[{i}].housekeeping must be a list of strings")

        coordinator_only_files = entry.get("coordinator_only_files", []) or []
        if not isinstance(coordinator_only_files, list) or not all(isinstance(f, str) for f in coordinator_only_files):
            raise ConfigError(f"repos[{i}].coordinator_only_files must be a list of strings")

        # #268: reference_repos — sibling repos a worker may reference
        # for context but doesn't actually build against.
        reference_repos = entry.get("reference_repos", []) or []
        if not isinstance(reference_repos, list) or not all(isinstance(r, str) for r in reference_repos):
            raise ConfigError(f"repos[{i}].reference_repos must be a list of repo names")

        repos.append(
            Repo(
                name=name,
                github=github,
                depends_on=depends_on,
                default_branch=default_branch,
                build_command=build_command,
                test_command=test_command,
                run_cmd=run_cmd,
                worker_permissions=worker_permissions,
                housekeeping=housekeeping,
                coordinator_only_files=coordinator_only_files,
                reference_repos=reference_repos,
            )
        )
    return repos


def _parse_worker_permissions(raw: Any, repo_index: int) -> WorkerPermissionsConfig:
    """Parse the ``worker_permissions`` block for a single repo.

    When *raw* is ``None`` (key absent from YAML), the default deny-list is
    applied — safety by default.  An explicit ``deny: []`` clears restrictions.
    """
    if raw is None:
        return WorkerPermissionsConfig(deny=list(DEFAULT_DENY_COMMANDS))

    if not isinstance(raw, dict):
        raise ConfigError(
            f"repos[{repo_index}].worker_permissions must be a mapping"
        )

    allow = raw.get("allow", []) or []
    if not isinstance(allow, list) or not all(isinstance(a, str) for a in allow):
        raise ConfigError(
            f"repos[{repo_index}].worker_permissions.allow must be a list of strings"
        )

    deny = raw.get("deny", []) or []
    if not isinstance(deny, list) or not all(isinstance(d, str) for d in deny):
        raise ConfigError(
            f"repos[{repo_index}].worker_permissions.deny must be a list of strings"
        )

    return WorkerPermissionsConfig(allow=allow, deny=deny)


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


def _parse_concurrency(raw: Any) -> ConcurrencyConfig:
    if raw is None:
        return ConcurrencyConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'concurrency' must be a mapping")
    cfg = ConcurrencyConfig()
    for key in (
        "max_workers", "stagger_seconds", "backoff_base", "max_retries",
        "stale_threshold", "first_output_timeout",
    ):
        val = raw.get(key)
        if val is None:
            continue
        if key in ("max_retries", "max_workers", "stale_threshold"):
            if not isinstance(val, int) or val < 0:
                raise ConfigError(f"concurrency.{key} must be a non-negative integer")
        else:
            # bool is a subclass of int — reject it explicitly for numeric keys.
            if isinstance(val, bool) or not isinstance(val, (int, float)) or val < 0:
                raise ConfigError(f"concurrency.{key} must be a non-negative number")
        setattr(cfg, key, val)
    if "auto_reassign" in raw:
        val = raw["auto_reassign"]
        if not isinstance(val, bool):
            raise ConfigError("concurrency.auto_reassign must be a boolean")
        cfg.auto_reassign = val
    if "bash_wrap_spawn" in raw:
        val = raw["bash_wrap_spawn"]
        if not isinstance(val, bool):
            raise ConfigError("concurrency.bash_wrap_spawn must be a boolean")
        cfg.bash_wrap_spawn = val
    return cfg


def _parse_smoke_tests(raw: Any) -> SmokeTestsConfig:
    if raw is None:
        return SmokeTestsConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'smoke_tests' must be a mapping")

    cfg = SmokeTestsConfig()
    if "auto_queue" in raw:
        value = raw["auto_queue"]
        if not isinstance(value, bool):
            raise ConfigError("smoke_tests.auto_queue must be a boolean")
        cfg.auto_queue = value

    if "default_command" in raw:
        value = raw["default_command"]
        if value is not None and not isinstance(value, str):
            raise ConfigError("smoke_tests.default_command must be a string")
        cfg.default_command = value

    if "timeout_seconds" in raw:
        value = raw["timeout_seconds"]
        if not isinstance(value, int) or value <= 0:
            raise ConfigError("smoke_tests.timeout_seconds must be a positive integer")
        cfg.timeout_seconds = value

    rules_raw = raw.get("capability_rules", []) or []
    if not isinstance(rules_raw, list):
        raise ConfigError("smoke_tests.capability_rules must be a list")
    rules: list[SmokeRule] = []
    for i, entry in enumerate(rules_raw):
        if not isinstance(entry, dict):
            raise ConfigError(
                f"smoke_tests.capability_rules[{i}] must be a mapping"
            )
        files = entry.get("files", []) or []
        requires = entry.get("requires", []) or []
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            raise ConfigError(
                f"smoke_tests.capability_rules[{i}].files must be a list of strings"
            )
        if not isinstance(requires, list) or not all(isinstance(r, str) for r in requires):
            raise ConfigError(
                f"smoke_tests.capability_rules[{i}].requires must be a list of strings"
            )
        if not files:
            raise ConfigError(
                f"smoke_tests.capability_rules[{i}].files must be non-empty"
            )
        if not requires:
            raise ConfigError(
                f"smoke_tests.capability_rules[{i}].requires must be non-empty"
            )
        rules.append(SmokeRule(files=files, requires=requires))
    cfg.capability_rules = rules
    return cfg


def _parse_models(raw: Any) -> ModelsConfig:
    if raw is None:
        return ModelsConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'models' must be a mapping")

    cfg = ModelsConfig()
    if "default" in raw:
        value = raw["default"]
        if not isinstance(value, str) or not value:
            raise ConfigError("models.default must be a non-empty string")
        cfg.default = value

    if "escalation" in raw:
        value = raw["escalation"]
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ConfigError("models.escalation must be a list of non-empty strings")
        cfg.escalation = list(value)

    if "labels" in raw:
        value = raw["labels"]
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        ):
            raise ConfigError(
                "models.labels must be a mapping of label name → model alias"
            )
        cfg.labels = dict(value)

    if "versions" in raw:
        value = raw["versions"]
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and k and isinstance(v, str) and v
            for k, v in value.items()
        ):
            raise ConfigError(
                "models.versions must be a mapping of alias → exact model id"
            )
        cfg.versions = dict(value)

    return cfg


def _parse_pipeline(raw: Any) -> PipelineConfig:
    if raw is None:
        return PipelineConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'pipeline' must be a mapping")

    cfg = PipelineConfig()

    if "default_gates" in raw:
        value = raw["default_gates"]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError("pipeline.default_gates must be a list of strings")
        cfg.default_gates = list(value)

    if "labels" in raw:
        value = raw["labels"]
        if not isinstance(value, dict):
            raise ConfigError("pipeline.labels must be a mapping of label → list of strings")
        for k, v in value.items():
            if not isinstance(k, str):
                raise ConfigError("pipeline.labels keys must be strings")
            if not isinstance(v, list) or not all(isinstance(g, str) for g in v):
                raise ConfigError(
                    f"pipeline.labels[{k!r}] must be a list of gate name strings"
                )
        cfg.labels = {k: list(v) for k, v in value.items()}

    if "auto_loop" in raw:
        value = raw["auto_loop"]
        if not isinstance(value, bool):
            raise ConfigError("pipeline.auto_loop must be a boolean")
        cfg.auto_loop = value

    if "max_review_iterations" in raw:
        value = raw["max_review_iterations"]
        if not isinstance(value, int) or value < 1:
            raise ConfigError("pipeline.max_review_iterations must be a positive integer")
        cfg.max_review_iterations = value

    if "escalate_fix_model" in raw:
        value = raw["escalate_fix_model"]
        if not isinstance(value, bool):
            raise ConfigError("pipeline.escalate_fix_model must be a boolean")
        cfg.escalate_fix_model = value

    return cfg


def _parse_dispatch(raw: Any) -> DispatchConfig:
    if raw is None:
        return DispatchConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'dispatch' must be a mapping")

    cfg = DispatchConfig()

    if "max_files_per_worker" in raw:
        value = raw["max_files_per_worker"]
        if not isinstance(value, int) or value < 1:
            raise ConfigError("dispatch.max_files_per_worker must be a positive integer")
        cfg.max_files_per_worker = value

    if "auto_split" in raw:
        value = raw["auto_split"]
        if not isinstance(value, bool):
            raise ConfigError("dispatch.auto_split must be a boolean")
        cfg.auto_split = value

    if "require_plan" in raw:
        value = raw["require_plan"]
        if not isinstance(value, bool):
            raise ConfigError("dispatch.require_plan must be a boolean")
        cfg.require_plan = value

    return cfg


def _parse_ci_store(raw: Any) -> CiStoreConfig:
    if raw is None:
        return CiStoreConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'ci_store' must be a mapping")

    cfg = CiStoreConfig()
    if "type" in raw:
        value = raw["type"]
        if not isinstance(value, str) or value not in ("github", "none"):
            raise ConfigError("ci_store.type must be one of: github, none")
        cfg.type = value
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

        # #268: reference_repos go through the same name-resolution as
        # depends_on but DO NOT feed into the cycle detector — the
        # intent is precisely to allow back-references (vimcode →
        # quadraui in depends_on; quadraui → vimcode in reference_repos)
        # that would be cycles if treated as build deps.
        unknown_ref = [r2 for r2 in r.reference_repos if r2 not in repo_names]
        if unknown_ref:
            raise ConfigError(
                f"repo {r.name!r} reference_repos unknown repos: {unknown_ref}"
            )
        if r.name in r.reference_repos:
            raise ConfigError(
                f"repo {r.name!r} cannot reference itself"
            )

    cycles = detect_cycles(repos)
    if cycles:
        cycle_str = " → ".join(cycles[0])
        raise ConfigError(f"circular dependency detected: {cycle_str}")
