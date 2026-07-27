"""Core ``coord acceptance`` orchestration (#944, docs/ORACLE_LOOP.md).

Pure/testable logic shared by the ``coord acceptance run`` / ``record`` CLI
commands in ``coord/commands/acceptance.py``: manifest loading (test-id ->
issue slice mapping) and building the structured verdict payload from a
driver's parsed test results.  Kept separate from the CLI so it can be unit
tested without Click's invocation machinery, mirroring the
``test_orchestrator.py`` / ``commands/test_gate.py`` split.

Layout this module expects (docs/ORACLE_LOOP.md "Layout"):

    tests/acceptance/ms-NN/
        contract.md          # black-box surface (not read by this module)
        mocks/                # viewable mocks == assertion fixtures
        <suite files>         # SEALED to the worker
        manifest.(yml|json)   # test-id -> issue-slice mapping
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from coord.config import Config
from coord.models import Machine, Repo

ACCEPTANCE_DIRNAME = "tests/acceptance"


def ms_dirname(milestone_number: int) -> str:
    """The ``ms-NN`` directory name for *milestone_number* (docs/ORACLE_LOOP.md
    "Layout"). Single source of truth for the naming convention so Gate A
    (#930, ``coord acceptance mock``) and the manifest reader agree."""
    return f"ms-{milestone_number}"


def gate_a_contract_path(milestone_number: int) -> str:
    """Repo-relative path to *milestone_number*'s Gate A contract
    (docs/ORACLE_LOOP.md "Layout": ``tests/acceptance/ms-NN/contract.md``).

    Used both by ``coord acceptance mock`` (#930, what it writes) and
    ``coord.milestone_dispatch.gate_a_status`` (what it checks for before
    letting the milestone's issues dispatch).
    """
    return f"{ACCEPTANCE_DIRNAME}/{ms_dirname(milestone_number)}/contract.md"


def _mocks_dir(milestone_number: int) -> str:
    return f"{ACCEPTANCE_DIRNAME}/{ms_dirname(milestone_number)}/mocks"


# Mock-fixture file extension -> the driver ``kind`` it implies (the SAME
# rule each ``AcceptanceDriverConfig.mock`` glob already encodes in
# coordinator.yml / docs/ORACLE_LOOP.md: ``"*.screen"`` for ``tui-tuidriver``,
# ``"*.out"`` for ``cli-pytest``). Single source of truth for the
# mock-kind -> ``--for-path`` derivation (#1453 review) — do not re-derive
# this mapping a second time anywhere else.
MOCK_EXT_TO_DRIVER_KIND: dict[str, str] = {
    ".screen": "tui-tuidriver",
    ".out": "cli-pytest",
}


class ForPathResolutionError(Exception):
    """A routed repo's ``--for-path`` could not be resolved unambiguously
    from a milestone's Gate-A mocks. Message is operator-facing."""


# (repo_github, dir_path, branch) -> filenames (not full paths) directly
# under that directory, or () when it doesn't exist. Injected so tests never
# hit `gh` — mirrors ``coord.milestone_dispatch.GateAFileExists``.
MockLister = Callable[[str, str, str], "tuple[str, ...]"]


def _default_list_mock_dir(repo_github: str, path: str, branch: str) -> "tuple[str, ...]":
    from coord import github_ops  # noqa: PLC0415

    try:
        return tuple(github_ops.list_repo_dir(repo_github, path, branch=branch))
    except RuntimeError:
        return ()


def resolve_for_path(
    config: Config,
    repo_cfg: Repo,
    milestone_number: int,
    *,
    list_mock_dir: MockLister | None = None,
) -> str | None:
    """Derive the ``--for-path`` glob a ROUTED repo's JIT acceptance-author
    dispatch needs, from *milestone_number*'s Gate-A mock file kind.

    SHARED helper (#1453 review finding 1, tracked for #1460's TUI-menu
    equivalent too — do not duplicate this rule): the mock fixtures a
    milestone's Gate-A contract ships under ``tests/acceptance/ms-NN/mocks/``
    (already merged to the default branch by the time this is ever called —
    :func:`coord.milestone_dispatch.gate_a_status` gates on exactly that)
    have a file extension that implies exactly one driver ``kind``
    (:data:`MOCK_EXT_TO_DRIVER_KIND`). Crossing that against
    ``acceptance.drivers.<repo>.routes[].kind`` picks the one route whose
    ``match`` glob is this milestone's ``--for-path``.

    Returns:
    - ``None`` when *repo_cfg* has no acceptance driver at all, or a FLAT
      (unrouted) one — :meth:`coord.config.AcceptanceConfig.driver_for`
      already resolves those with no path, so no ``--for-path`` is needed.
    - the single matching route's ``match`` glob when resolution is
      unambiguous.

    Raises :class:`ForPathResolutionError` (operator-facing, mirrors
    ``coord.test_author.dispatch_test_author``'s "no route matched" message)
    when the repo IS routed but resolution is ambiguous — no mocks found,
    more than one mock kind present, or zero/more-than-one route declares
    the implied kind. Callers should surface this rather than guess.
    """
    entry = config.acceptance.drivers.get(repo_cfg.name)
    if entry is None or not entry.routes:
        return None

    lister = list_mock_dir or _default_list_mock_dir
    mocks_dir = _mocks_dir(milestone_number)
    names = lister(repo_cfg.github, mocks_dir, repo_cfg.default_branch)

    kinds = {
        MOCK_EXT_TO_DRIVER_KIND[Path(name).suffix]
        for name in names
        if Path(name).suffix in MOCK_EXT_TO_DRIVER_KIND
    }

    def _refuse(reason: str) -> "ForPathResolutionError":
        routes = ", ".join(f"{r.match!r} ({r.kind})" for r in entry.routes)
        return ForPathResolutionError(
            f"repo {repo_cfg.name!r} has a routed acceptance driver ({routes}) "
            f"but --for-path could not be derived from {mocks_dir!r}'s mock "
            f"kind: {reason}. Pass --no-acceptance to skip JIT authoring, or "
            f"dispatch by hand: coord acceptance author {repo_cfg.name} "
            "<tracking_issue> --issue <N> --for-path <glob>"
        )

    if not kinds:
        raise _refuse(f"no recognized mock files found ({names!r})")
    if len(kinds) > 1:
        raise _refuse(f"mixed mock kinds found ({sorted(kinds)!r})")
    kind = next(iter(kinds))

    matches = [route.match for route in entry.routes if route.kind == kind]
    if len(matches) != 1:
        raise _refuse(
            f"mock kind {kind!r} matches {len(matches)} routes, need exactly 1"
        )
    return matches[0]


class ManifestError(Exception):
    """Raised when a manifest file exists but is malformed."""


def _manifest_paths(acceptance_root: Path) -> list[Path]:
    """``ms-NN/manifest.(yml|yaml|json)`` paths under *acceptance_root*,
    sorted for deterministic scan order. ``[]`` when the dir doesn't exist."""
    if not acceptance_root.exists():
        return []
    return sorted(
        p for p in acceptance_root.glob("*/manifest.*")
        if p.suffix in (".yml", ".yaml", ".json")
    )


@dataclass(frozen=True)
class ManifestData:
    """One manifest file's parsed contents: the test-id -> issue-number
    mapping, plus the #1138 issue-level ``exempt:`` list (issues in an
    oracle-opted-in milestone that are deliberately validated by their own
    unit tests instead of the sealed suite — e.g. the driver-building issue
    itself, #1125 — declared here instead of living only as tribal knowledge
    in an issue body)."""

    tests: dict[str, int] = field(default_factory=dict)
    exempt: frozenset[int] = field(default_factory=frozenset)


def parse_manifest_text(text: str, *, source: str = "<manifest>") -> ManifestData:
    """Pure parse of one manifest file's YAML/JSON text into
    :class:`ManifestData`. Shared by the local-disk loader
    (:func:`_parse_manifest_file`, used by ``coord acceptance run``/
    ``record`` on a worker's own checkout) and the dispatch-time GitHub-fetch
    reader (:func:`coord.milestone_dispatch.issue_oracle_ready`, #1138) so
    both agree on the manifest schema.

    Three on-disk shapes are accepted:

    - ``tests: {<test-id>: <issue-number>, ...}`` — flat, one issue per test.
    - ``issues: {<issue-number>: [<test-id>, ...], ...}`` — grouped by issue.
    - ``exempt: [<issue-number>, ...]`` — issues exempted from the #1138
      issue-level oracle gate (no slice required before Work dispatch).
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ManifestError(f"failed to parse manifest {source}: {e}") from e
    if raw is None:
        return ManifestData()
    if not isinstance(raw, dict):
        raise ManifestError(f"manifest {source} must be a mapping")

    mapping: dict[str, int] = {}
    tests_raw = raw.get("tests")
    if isinstance(tests_raw, dict):
        for test_id, issue in tests_raw.items():
            mapping[str(test_id)] = int(issue)

    issues_raw = raw.get("issues")
    if isinstance(issues_raw, dict):
        for issue, test_ids in issues_raw.items():
            if not isinstance(test_ids, list):
                continue
            for test_id in test_ids:
                mapping[str(test_id)] = int(issue)

    exempt: frozenset[int] = frozenset()
    exempt_raw = raw.get("exempt")
    if isinstance(exempt_raw, list):
        exempt = frozenset(int(x) for x in exempt_raw)

    return ManifestData(tests=mapping, exempt=exempt)


def _parse_manifest_file(path: Path) -> dict[str, int]:
    """Parse one manifest file into ``{test_id: issue_number}`` (the
    ``exempt:`` list, if any, is dropped — callers that need it use
    :func:`parse_manifest_text` directly). Two on-disk shapes are accepted:

    - ``tests: {<test-id>: <issue-number>, ...}`` — flat, one issue per test.
    - ``issues: {<issue-number>: [<test-id>, ...], ...}`` — grouped by issue.
    """
    try:
        text = path.read_text()
    except OSError as e:
        raise ManifestError(f"failed to parse manifest {path}: {e}") from e
    return parse_manifest_text(text, source=str(path)).tests


def load_manifest(acceptance_root: Path) -> dict[str, int]:
    """Merge every ``ms-NN/manifest.(yml|json)`` under *acceptance_root* into
    one ``{test_id: issue_number}`` mapping.

    Returns ``{}`` when *acceptance_root* doesn't exist or has no manifest
    files yet (the suite hasn't been authored — sibling issue #931). Later
    manifests win on a test-id collision (last one scanned, sorted by path
    for determinism) rather than raising, since two milestones legitimately
    sharing a test id is an authoring bug, not something this reader should
    crash the whole run over.
    """
    mapping: dict[str, int] = {}
    for path in _manifest_paths(acceptance_root):
        mapping.update(_parse_manifest_file(path))
    return mapping


def ms_dir_for_issue(acceptance_root: Path, issue_number: int) -> str | None:
    """The ``ms-NN`` directory name (under *acceptance_root*) whose manifest
    covers *issue_number*, or ``None`` if no manifest maps any test to it yet
    (the issue's slice hasn't been authored — #945 uses this to decide
    whether there's a contract to point the worker at).

    Unlike :func:`load_manifest`, this checks manifests **per file** rather
    than merging first, since the whole point is recovering *which* ``ms-NN``
    dir a given issue's tests live under.
    """
    for path in _manifest_paths(acceptance_root):
        mapping = _parse_manifest_file(path)
        if test_ids_for_issue(mapping, issue_number):
            return path.parent.name
    return None


def oracle_loop_contract_block(
    acceptance_root: Path, repo_name: str, issue_number: int
) -> str:
    """The worker briefing contract (#945, docs/ORACLE_LOOP.md "The worker
    briefing contract") prepended to the TOP of a Work briefing when
    *issue_number* has a sealed acceptance slice authored for it under
    *acceptance_root*.

    Returns ``""`` when the issue has no authored slice yet (nothing to
    point the worker at — Gate A/#931 hasn't run for it) or on any read
    error. Fully fail-soft — mirrors ``coord.state.issue_context_block``
    (#603): this runs on the dispatch hot path, so a manifest hiccup must
    degrade to "no block" rather than break dispatch.
    """
    try:
        ms_dir = ms_dir_for_issue(acceptance_root, issue_number)
    except Exception:  # noqa: BLE001 — never let a manifest read break dispatch
        return ""
    if ms_dir is None:
        return ""

    contract_path = f"{ACCEPTANCE_DIRNAME}/{ms_dir}/contract.md"
    return (
        "## 🔒 Oracle-loop acceptance contract — READ THIS FIRST\n\n"
        "This issue has a sealed acceptance slice authored for it. Treat "
        f"`{contract_path}` (the black-box surface) as the spec — not "
        "guesswork.\n\n"
        f"- You **may not** edit `{ACCEPTANCE_DIRNAME}/**`. It is the sealed "
        "oracle, authored independently of your work — touching it fails "
        "the gate.\n"
        f"- Run `coord acceptance run --repo {repo_name} --issue "
        f"{issue_number}` to check yourself; iterate in this warm session "
        "until your slice is green, then release.\n"
        "- Write your own unit / internal tests too — that is still your "
        "job.\n"
        "- If your slice won't converge — the failing set churns rather "
        "than shrinks across 2 rounds — **stop grinding**: run "
        f"`coord acceptance stall --repo {repo_name} --issue {issue_number} "
        '--tried "..." --stuck "..."` (#846) so the coordinator sees it '
        "immediately, in addition to a `STUCK:` line for the interactive "
        "log.\n\n"
        "---\n\n"
    )


def test_ids_for_issue(manifest: dict[str, int], issue_number: int) -> set[str]:
    """The set of test ids mapped to *issue_number* in *manifest*."""
    return {test_id for test_id, issue in manifest.items() if issue == issue_number}


def build_verdict(
    tests: list[dict],
    *,
    scope: str,
    issue_number: int | None = None,
) -> dict[str, Any]:
    """Assemble the structured pass/fail payload ``coord acceptance run``
    prints and ``record`` persists a summary of.

    *tests* is the (already filtered, when scoped to one issue) list of
    ``{"id", "status", "message"}`` dicts from a driver. Sealed: this only
    ever carries verdicts (id/status/message), never test source.
    """
    passed = sum(1 for t in tests if t.get("status") == "pass")
    failed = sum(1 for t in tests if t.get("status") == "fail")
    skipped = sum(1 for t in tests if t.get("status") == "skip")
    payload: dict[str, Any] = {
        "scope": scope,
        "total": len(tests),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "green": failed == 0 and len(tests) > 0,
        "tests": tests,
    }
    if issue_number is not None:
        payload["issue"] = issue_number
    return payload


def failure_summary(verdict: dict[str, Any], *, limit: int = 5) -> str:
    """One-line-per-failure summary text for a verdict payload (used as the
    Acceptance-gate reason string and the #603 durable-context note)."""
    failing = [t for t in verdict.get("tests", []) if t.get("status") == "fail"]
    if not failing:
        return ""
    lines = [f"{t['id']}: {t.get('message') or 'failed'}" for t in failing[:limit]]
    if len(failing) > limit:
        lines.append(f"... and {len(failing) - limit} more")
    return "\n".join(lines)


def dump_manifest_error_hint(acceptance_root: Path) -> str:
    """Human-facing hint for "no manifest found" — points at the authoring
    step (#931) rather than leaving the operator guessing."""
    return (
        f"no acceptance manifest found under {acceptance_root} — the sealed "
        "suite has not been authored yet for this repo (see docs/ORACLE_LOOP.md "
        "/ #931)."
    )


def acceptance_capability_gap(
    capability: str, repo_name: str, config: Config,
) -> Machine | None:
    """Detect a capability-matched-routing gap for an acceptance driver run
    (#966, deferred from #932/#944).

    ``coord acceptance run --all`` (Gate C) and ``coord acceptance record``
    always execute the driver's ``run`` command on whatever host invoked
    them — there is no remote-exec plumbing to actually route the run
    elsewhere yet (that's the "new plumbing, not a copy-paste" part #966
    defers until a driver with a real capability mismatch exists). What this
    function *can* do cheaply — mirroring ``coord.smoke.pick_smoke_machine``'s
    candidate filter, minus the async/busy-machine bits that don't apply to
    a synchronous command — is detect when it's about to run on the *wrong*
    hardware, so the caller can fail loudly instead of silently.

    Returns the first other configured machine that has *repo_name* and
    *capability*, when:
    - *capability* is set (drivers without one, e.g. today's only real
      driver's implicit local-only assumption, are never gapped), AND
    - this host is a recognized machine in ``coordinator.yml`` that does
      NOT have *capability* (an unrecognized host is given the benefit of
      the doubt — it might be a dev machine outside the fleet that happens
      to have everything installed), AND
    - some other configured machine actually has it (nothing to route to
      otherwise, so failing wouldn't be actionable).

    Returns ``None`` in every other case — i.e. "no known gap, proceed."
    """
    if not capability:
        return None

    from coord.test_orchestrator import local_machine  # noqa: PLC0415 — avoid import cycle

    here = local_machine(config)
    if here is None or capability in here.capabilities:
        return None

    candidates = [
        m for m in config.machines
        if m.can_work_on(repo_name) and capability in m.capabilities
    ]
    if not candidates:
        return None
    return candidates[0]
