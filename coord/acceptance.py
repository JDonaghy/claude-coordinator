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
import re
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


def issue_dirname(issue_number: int) -> str:
    """The ``issue-NN`` directory name for a single-issue bug-lane contract
    (docs/TEST_FIRST_BUG_LANE.md "The intake contract", #1964) — the bug
    lane's counterpart to :func:`ms_dirname`, with no milestone in the name
    because a bug has none.

    This is purely a naming convention, not new plumbing: the manifest
    scanner below (:func:`_manifest_paths`) globs ``*/manifest.*`` under
    ``tests/acceptance/`` regardless of what the directory is called, so an
    ``issue-NN/`` slice is discovered, run (``coord acceptance run --issue
    N``), recorded (``coord acceptance record``), and injected into the
    worker's briefing (:func:`oracle_loop_contract_block`) by the exact same
    code path as an ``ms-NN/`` one — see ``TestOracleLoopContractBlock`` in
    ``tests/test_acceptance.py``, which already proves the block is built
    from whatever the owning directory happens to be named. Pinning the name
    here just keeps every bug-lane contract in the same shape.
    """
    return f"issue-{issue_number}"


def bug_contract_path(issue_number: int) -> str:
    """Repo-relative path to *issue_number*'s single-issue bug-lane contract
    (docs/TEST_FIRST_BUG_LANE.md "The intake contract").

    Unlike :func:`gate_a_contract_path`, nothing gates dispatch on this
    existing — a bug issue has no milestone, so there is no Gate A to block
    on it. It is hand-authored (or agent-assisted) directly from the four
    intake fields (:mod:`coord.bug_intake`); once it — and a
    ``manifest.yml`` alongside it — exist, the issue behaves exactly like an
    authored ``ms-NN`` slice to every downstream command.
    """
    return f"{ACCEPTANCE_DIRNAME}/{issue_dirname(issue_number)}/contract.md"


# Mock-fixture file extension -> the driver ``kind`` it implies (the SAME
# rule each ``AcceptanceDriverConfig.mock`` glob already encodes in
# coordinator.yml / docs/ORACLE_LOOP.md: ``"*.screen"`` for ``tui-tuidriver``,
# ``"*.out"`` for ``cli-pytest``, ``"*.html"`` for ``web-playwright`` (#1542
# — hand-authored, self-contained wireframes; see
# ``coord.agent.MOCK_AUTHOR_SYSTEM_PROMPT`` for the authoring rules and
# ``tests/acceptance/ms-example/mocks/`` for a worked example). Single
# source of truth for the mock-kind -> ``--for-path`` derivation (#1453
# review) — do not re-derive this mapping a second time anywhere else.
MOCK_EXT_TO_DRIVER_KIND: dict[str, str] = {
    ".screen": "tui-tuidriver",
    ".out": "cli-pytest",
    ".html": "web-playwright",
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
    in an issue body).

    #2063 adds the milestone-level ``gate_a:`` block: the declared,
    reviewable opt-out from the Gate-A human sign-off gate, same posture as
    ``exempt:`` above — a milestone whose surface genuinely needs no human
    eye says so in the repo, in writing, rather than the gate quietly not
    existing for everyone.

    #2164 adds ``expected_red``: ``{issue_number: {test_id, ...}}``, the
    registry of test-ids a sealed slice is *known* to fail before its fix
    exists. A test-id listed there that fails is not a CI failure
    (:func:`apply_expected_red`); one that PASSES is a hard, loud failure —
    the vacuous-assertion case #1965 cares about. Cleared only by ``coord
    acceptance record`` observing green externally
    (:func:`clear_expected_red_entries`), never by a worker edit."""

    tests: dict[str, int] = field(default_factory=dict)
    exempt: frozenset[int] = field(default_factory=frozenset)
    #: ``gate_a: {exempt: true, reason: "..."}`` — this milestone's contract
    #: may be consumed without a recorded human verdict (#2063).
    gate_a_exempt: bool = False
    gate_a_exempt_reason: str = ""
    #: #2164 — see the class docstring's ``expected_red`` paragraph.
    expected_red: "dict[int, frozenset[str]]" = field(default_factory=dict)


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

    Plus two milestone-level blocks:

    - ``gate_a: {exempt: true, reason: "..."}`` — this milestone's contract
      may be consumed without a recorded human sign-off (#2063). ``gate_a:
      true`` is accepted as shorthand. Anything else (including a missing
      key) leaves the gate on.
    - ``expected_red: {<issue_number>: [<test-id>, ...], ...}`` (#2164) — the
      test-ids a sealed slice is authored to fail *right now*, before its
      fix exists. Non-dict/non-list entries are ignored rather than raising
      — a malformed ``expected_red`` block degrades to "nothing is
      expected-red" (fails toward the stricter, ordinary-CI behavior)
      instead of blowing up the parse.
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

    gate_a_exempt = False
    gate_a_reason = ""
    gate_a_raw = raw.get("gate_a")
    if isinstance(gate_a_raw, dict):
        gate_a_exempt = bool(gate_a_raw.get("exempt"))
        gate_a_reason = str(gate_a_raw.get("reason") or "")
    elif isinstance(gate_a_raw, bool):
        gate_a_exempt = gate_a_raw

    expected_red: dict[int, frozenset[str]] = {}
    expected_red_raw = raw.get("expected_red")
    if isinstance(expected_red_raw, dict):
        for issue, test_ids in expected_red_raw.items():
            if not isinstance(test_ids, list):
                continue
            try:
                issue_num = int(issue)
            except (TypeError, ValueError):
                continue
            expected_red[issue_num] = frozenset(str(t) for t in test_ids)

    return ManifestData(
        tests=mapping,
        exempt=exempt,
        gate_a_exempt=gate_a_exempt,
        gate_a_exempt_reason=gate_a_reason,
        expected_red=expected_red,
    )


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


def load_expected_red(acceptance_root: Path) -> dict[str, int]:
    """Merge every ``ms-NN/manifest.(yml|json)``'s ``expected_red:`` block
    under *acceptance_root* into one ``{test_id: issue_number}`` mapping
    (#2164) — the flat shape :func:`apply_expected_red` and the ``coord
    acceptance run --all --ci`` CI wrapper consume.

    Mirrors :func:`load_manifest`'s merge/empty-dict/last-writer-wins
    conventions exactly, just reading ``expected_red`` instead of ``tests``.
    """
    mapping: dict[str, int] = {}
    for path in _manifest_paths(acceptance_root):
        try:
            data = parse_manifest_text(path.read_text(), source=str(path))
        except OSError as e:
            raise ManifestError(f"failed to parse manifest {path}: {e}") from e
        for issue_number, test_ids in data.expected_red.items():
            for test_id in test_ids:
                mapping[test_id] = issue_number
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
    mocks_dir = f"{ACCEPTANCE_DIRNAME}/{ms_dir}/mocks"
    return (
        "## 🔒 Oracle-loop acceptance contract — READ THIS FIRST\n\n"
        "This issue has a sealed acceptance slice authored for it. Treat "
        f"`{contract_path}` (the black-box surface) — and, if present, "
        f"the rendered mock(s) under `{mocks_dir}/` — as the spec — not "
        "guesswork. For a web slice the mocks ARE part of the contract "
        "(hand-authored HTML wireframes, one per screen state): the app "
        "must satisfy the sealed assertions written against them, not the "
        "other way around.\n\n"
        f"- You **may not** edit `{ACCEPTANCE_DIRNAME}/**` (contract, "
        "mocks, or the sealed suite). It is the sealed oracle, authored "
        "independently of your work — touching it fails the gate.\n"
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


def apply_expected_red(verdict: dict[str, Any], expected_red_ids: "set[str]") -> dict[str, Any]:
    """Mutate + return *verdict* (from :func:`build_verdict`) with the
    #2164 expected-red accounting the CI wrapper (``coord acceptance run
    --all --ci``) needs, on top of the raw ``green`` field callers already
    relied on before this existed.

    Adds:

    - ``expected_red_still_red``: ids in *expected_red_ids* that failed —
      the ordinary, designed-for case. Excluded from ``ci_green``'s failure
      count.
    - ``unexpected_green``: ids in *expected_red_ids* that PASSED — the
      loud, distinguishable hard failure this registry exists to catch
      (#1965's "an assertion that never exercised the bug"). A single one
      of these is enough to fail ``ci_green`` even if every other test is
      green.
    - ``ci_green``: true iff there are zero real (non-expected-red)
      failures AND zero unexpected-green ids AND at least one test ran.
      This — not the raw ``green`` — is what a CI gate should key off of;
      ``green`` is left untouched so existing (non-CI) callers of
      :func:`build_verdict` see no behavior change.

    A no-op (``ci_green == green``, both new lists empty) when
    *expected_red_ids* is empty — the overwhelmingly common case (most
    slices have nothing expected-red).
    """
    tests = verdict.get("tests", [])
    if not expected_red_ids:
        verdict["expected_red_still_red"] = []
        verdict["unexpected_green"] = []
        verdict["ci_green"] = verdict["green"]
        return verdict

    unexpected_green = sorted(
        t["id"] for t in tests if t.get("status") == "pass" and t["id"] in expected_red_ids
    )
    expected_red_still_red = sorted(
        t["id"] for t in tests if t.get("status") == "fail" and t["id"] in expected_red_ids
    )
    real_failures = sum(
        1 for t in tests if t.get("status") == "fail" and t["id"] not in expected_red_ids
    )
    verdict["unexpected_green"] = unexpected_green
    verdict["expected_red_still_red"] = expected_red_still_red
    verdict["ci_green"] = (
        len(tests) > 0 and real_failures == 0 and not unexpected_green
    )
    return verdict


def expected_red_failure_summary(verdict: dict[str, Any]) -> str:
    """Loud, distinguishable-from-an-ordinary-failure message for a verdict
    whose ``unexpected_green`` (from :func:`apply_expected_red`) is
    non-empty — a test-id the manifest says is ``expected_red`` but which
    just PASSED. Returns ``""`` when there's nothing to report.

    This is deliberately worded differently from :func:`failure_summary`'s
    per-test failure lines: the point (#1965) is that a human/CI reader
    can't mistake this for "a test failed" — it is the opposite signal, and
    the fix is editorial (clear the manifest entry, or realize the
    assertion never exercised the bug), not code.
    """
    ids = verdict.get("unexpected_green") or []
    if not ids:
        return ""
    listed = "\n".join(f"  - {i}" for i in ids)
    return (
        f"HARD FAILURE: {len(ids)} test(s) listed in `expected_red` now PASS:\n"
        f"{listed}\n"
        "An expected-red test that passes means either the fix already "
        "landed silently (clear it — `coord acceptance record` does this "
        "automatically on a green trust-gate run) or the assertion never "
        "exercised the bug in the first place (#1965). This is NOT an "
        "ordinary test failure — it is the opposite signal."
    )


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


def _is_content_line(line: str) -> bool:
    """True when *line*, with any ``#`` comment stripped, still has
    non-whitespace content — i.e. it's real YAML, not blank/comment-only."""
    return bool(line.split("#", 1)[0].strip())


_EXPECTED_RED_KEY_RE = re.compile(r"^expected_red\s*:\s*(#.*)?$")


def _issue_header_re(issue_number: int) -> re.Pattern[str]:
    return re.compile(rf"^(\s*)['\"]?{issue_number}['\"]?\s*:\s*(#.*)?$")


def _strip_cleared_ids_from_issue_block(
    body: "list[str]", issue_number: int, cleared_ids: "set[str]",
) -> "tuple[list[str], bool]":
    """Within one ``expected_red:`` block's lines (*body*), drop any list
    item under *issue_number* whose id is in *cleared_ids*; drop the whole
    ``<issue_number>:`` sub-block (header + remaining lines, including any
    now-orphaned comments) if nothing but comments/blanks are left under it.

    Returns ``(new_body, changed)``.
    """
    header_re = _issue_header_re(issue_number)
    out: list[str] = []
    changed = False
    i, n = 0, len(body)
    while i < n:
        line = body[i]
        m = header_re.match(line)
        if not m:
            out.append(line)
            i += 1
            continue
        header_indent = len(m.group(1))
        i += 1
        sub: list[str] = []
        while i < n:
            sub_line = body[i]
            sub_indent = len(sub_line) - len(sub_line.lstrip(" "))
            if sub_line.strip() and sub_indent <= header_indent:
                break
            sub.append(sub_line)
            i += 1
        new_sub = []
        for sub_line in sub:
            code = sub_line.split("#", 1)[0].strip()
            item_m = re.match(r"^-\s*(.+?)\s*$", code)
            if item_m and item_m.group(1) in cleared_ids:
                changed = True
                continue
            new_sub.append(sub_line)
        if any(_is_content_line(sub_line) for sub_line in new_sub):
            out.append(line)
            out.extend(new_sub)
        else:
            # Nothing but comments/blanks left under this issue — drop the
            # header too rather than leave a dangling `NNN:` with no items.
            changed = True
    return out, changed


def clear_expected_red_entries(
    text: str, issue_number: int, cleared_test_ids: "set[str]",
) -> str | None:
    """Pure text-surgery (#2164): remove *cleared_test_ids* from
    *issue_number*'s list under the ``expected_red:`` block of a
    ``manifest.yml``'s raw *text*, preserving every other line — including
    comments — byte-for-byte.

    Used by ``coord acceptance record``'s trust-gate clearing step (the
    coordinator, never a worker, observes a previously-expected-red test go
    green externally and drops it from the registry) — the manifest carries
    hand-written commentary (see ``tests/acceptance/ms-33/manifest.yml``)
    that a parse-and-``yaml.safe_dump`` round-trip would destroy, so this
    edits the text directly instead of going through :mod:`yaml`.

    Returns the updated text, or ``None`` when nothing changed (no matching
    id was found under *issue_number* — the caller should skip committing a
    no-op). If clearing empties an issue's whole list, that issue's
    sub-block (header + any orphaned comments) is dropped; if that empties
    the whole ``expected_red:`` block, the key itself is dropped too.
    """
    if not cleared_test_ids:
        return None

    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i, n = 0, len(lines)
    changed = False
    while i < n:
        line = lines[i]
        if _EXPECTED_RED_KEY_RE.match(line.strip()):
            i += 1
            body: list[str] = []
            while i < n:
                body_line = lines[i]
                indent = len(body_line) - len(body_line.lstrip(" "))
                if body_line.strip() and indent == 0:
                    break
                body.append(body_line)
                i += 1
            new_body, body_changed = _strip_cleared_ids_from_issue_block(
                body, issue_number, cleared_test_ids,
            )
            if body_changed:
                changed = True
            if any(_is_content_line(bl) for bl in new_body):
                out.append(line)
                out.extend(new_body)
            else:
                # Whole registry is now empty — drop the key too.
                changed = True
        else:
            out.append(line)
            i += 1

    if not changed:
        return None
    return "".join(out)
