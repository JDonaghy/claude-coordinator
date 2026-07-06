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
from pathlib import Path
from typing import Any

import yaml

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


def _parse_manifest_file(path: Path) -> dict[str, int]:
    """Parse one manifest file into ``{test_id: issue_number}``.

    Two on-disk shapes are accepted:

    - ``tests: {<test-id>: <issue-number>, ...}`` — flat, one issue per test.
    - ``issues: {<issue-number>: [<test-id>, ...], ...}`` — grouped by issue.
    """
    try:
        raw = yaml.safe_load(path.read_text())
    except (yaml.YAMLError, OSError) as e:
        raise ManifestError(f"failed to parse manifest {path}: {e}") from e
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ManifestError(f"manifest {path} must be a mapping")

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

    return mapping


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

    Note: the contract's "stop and report" step below intentionally says
    to use a ``STUCK:`` line rather than ``coord acceptance stall`` — the
    latter is #846 and not implemented yet. Update this text once #846
    ships a real ``coord acceptance stall`` command.
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
        "than shrinks across 2 rounds — **stop grinding**: report it in a "
        "`STUCK:` line with what you tried and the stuck test ids so the "
        "coordinator can intervene.\n\n"
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
