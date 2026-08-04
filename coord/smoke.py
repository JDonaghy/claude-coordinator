"""Smoke-test orchestration — auto-queue validation on a capable machine.

When a worker finishes, the work often needs validation hardware the worker
didn't have. Example: a GTK key-routing fix built on a no-GTK server needs a
machine with GTK to actually verify the popup works. This module:

1. Reads the worker's diff (which files changed).
2. Looks at `smoke_tests.capability_rules` — each rule maps a file-path
   prefix to a set of required machine capabilities.
3. Picks a machine that has all required capabilities, preferring one
   different from the worker.
4. Dispatches a `type="smoke"` assignment with a briefing that tells
   `claude -p` to fetch the branch, run the smoke command, and report
   pass/fail through its exit code.

Public entry points:

- `match_rules(touched_files, rules)`  — pure: returns the union of required
  capabilities for any rule whose `files` prefix matches a touched file.
- `rank_smoke_machines(required_caps, repo, worker_machine, board, config)` —
  every capability-matched machine, best first (#1672).
- `pick_smoke_machine(required_caps, worker_machine, board, config)` — picks
  a capable machine, preferring the worker's own (its build cache is warm —
  #1402); pass `prefer_worker=False` for the old different-machine-first
  order. Thin wrapper over `rank_smoke_machines` — the head of the ranking.
- `dispatch_smoke(completed, board, config, ...)` — the full path; called
  from reconcile when a work assignment transitions to done.

#1819: the unit a Test run measures is the **(branch, base)** pair, not the
work row that asked for it. Three guards in `dispatch_smoke` follow from that
— a branch-scoped in-flight dedupe, a supersession check that skips a work row
a later row replaced on the same branch, and a refusal to stamp the transient
`running` marker over a verdict that already exists. Together they are what
stops a fix round (which reuses the branch by design, so one branch carries
two `work` rows) from putting two machines on the identical suite and then
looping forever as each re-dispatch retracts the verdict the last one landed.

Why a separate module from `coord/review.py`: smoke tests target machine
capabilities (GTK/terminal/CUDA), not session independence. The selection
algorithm is different — for reviews we want a *different* machine for
independence; for smoke we want a *capable* machine for hardware, and
"different" is only a tie-breaker.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Callable

import httpx

from coord import github_ops
from coord.config import Config, SmokeRule, SmokeTestsConfig
from coord.dispatch import AGENT_PORT
from coord.models import WORK_LIKE_TYPES, Assignment, Board, Machine

logger = logging.getLogger("coord.smoke")


SMOKE_SYSTEM_PROMPT = """\
You are a smoke-test runner dispatched by the coordinator. \
Your only job: pull the branch, run the smoke command, report pass/fail.

Rules:
- Do NOT edit source files. Do NOT push commits. You only validate.
- You MAY perform test-environment SETUP the smoke command needs — creating
  a venv, `pip install`-ing dev deps, symlinking a sibling checkout for a
  path dependency (e.g. coord-tui's quadraui link), writing build artifacts.
  None of that touches the branch's source; it is exactly what the smoke
  command itself does when run locally, so do it without asking.
- Do NOT run `gh` commands. The coordinator owns GitHub interactions.
- You MAY run git, build commands, and test commands.
- Exit code is what the coordinator reads — exit 0 on pass, non-zero on \
fail. Print a final line `SMOKE: pass` or `SMOKE: fail <one-line reason>` \
before exiting so logs are readable.

Where you are:
- You are in a dedicated git worktree created for this run. Every git command \
below runs THERE, in your current directory.
- Do NOT `cd` to the machine's shared base checkout (`~/src/<repo>`), and do \
NOT `git checkout` / `git switch` inside it. Leaving that checkout parked on a \
feature branch makes every later dispatch against that branch on this machine \
fail (#1694).

Steps:
1. `git fetch origin && git checkout <branch>` (the branch is in your \
briefing) — in your worktree, never in the base checkout.
2. Run the smoke command from the briefing. Capture stdout/stderr.
3. If it exits 0 → print `SMOKE: pass` and exit 0.
4. If it fails → print `SMOKE: fail <short reason>` and exit non-zero.
"""


# ── Rule matching ───────────────────────────────────────────────────────────


def match_rules(touched_files: list[str], rules: list[SmokeRule]) -> list[str]:
    """Return the union of `requires` for any rule that any touched file hits.

    Matching is path-prefix: a rule with `files=["src/gtk/"]` matches
    `src/gtk/foo.c` but not `src/cli.py`. A rule with `files=["src/gtk"]`
    (no slash) catches both `src/gtk/foo.c` and `src/gtk_helpers.c` — use
    the trailing slash form to be strict.

    Returns capabilities in deterministic order (first-seen across rules).
    """
    seen: dict[str, None] = {}
    for path in touched_files:
        for rule in rules:
            if not any(path.startswith(pattern) for pattern in rule.files):
                continue
            for cap in rule.requires:
                seen.setdefault(cap, None)
    return list(seen.keys())


# ── Machine selection ───────────────────────────────────────────────────────


@dataclass
class SmokeMachineChoice:
    machine: Machine
    is_worker: bool
    rationale: str


def rank_smoke_machines(
    required_caps: list[str],
    repo_name: str,
    worker_machine_name: str,
    board: Board,
    config: Config,
    *,
    prefer_worker: bool = True,
) -> list[SmokeMachineChoice]:
    """Every machine that can smoke-test `repo_name` with all `required_caps`,
    best candidate first (#1672).

    Preference order (#1402):
    1. The worker's own machine, if capable and idle
    2. Idle, capable, different from worker (config order)
    3. The worker's own machine, if capable (busy — smoke will queue)
    4. Busy, capable, different from worker (config order; smoke will queue)

    Every candidate appears exactly once; the head of the list is exactly what
    :func:`pick_smoke_machine` used to return on its own.

    **Why a ranking and not a single pick (#1672).** ``dispatch_smoke`` has to
    reject a candidate *after* choosing it — its live ``/health`` probe can
    contradict the capabilities ``coordinator.yml`` declares for it (#1570 D),
    or it can turn out to have no ``repo_paths`` entry. Returning one machine
    meant a single bad candidate ended the whole Test stage: on 2026-08-01
    (#1678) the router picked the same unhealthy machine every 30 s forever
    while two other machines declared the same capability and were never
    tried. The caller now walks this list.

    **Capability matching is unchanged and is never relaxed.** Only machines
    that genuinely declare every required capability (and can work on the
    repo) are in the list at all — a fallback that dispatched to a machine
    lacking the capability would produce a green verdict from a machine that
    cannot run the suite, which is worse than refusing.

    **Why the worker machine is preferred.** This used to prefer a machine
    *different* from the worker.  That preference is right for **review**,
    where independence from the worker's context is the entire point — but a
    test run needs **capability**, not independence: it re-runs the suite
    against the pushed commit and the verdict is identical wherever it runs.
    Meanwhile the worker's machine is the one with a warm build cache
    (``coord.cargo_cache``, #1402) and, for a Rust repo, that is the
    difference between ~18 s and ~3 min.  Capability rules still bind
    absolutely: a GTK or browser suite goes to a capable machine even when
    the worker ran somewhere else, and a worker machine that lacks a required
    capability is never chosen.

    Pass ``prefer_worker=False`` to restore the different-machine-first
    ordering (used by callers that want independence, and by tests pinning
    the old behaviour).

    Returns an empty list when capabilities can't be matched.
    """
    candidates = [
        m for m in config.machines
        if m.can_work_on(repo_name)
        and all(cap in m.capabilities for cap in required_caps)
    ]
    if not candidates:
        return []

    busy = {a.machine_name for a in board.active if a.status in ("pending", "running")}

    same = next((m for m in candidates if m.name == worker_machine_name), None)

    ranked: list[SmokeMachineChoice] = []
    seen: set[str] = set()

    def _add(choice: SmokeMachineChoice) -> None:
        if choice.machine.name in seen:
            return
        seen.add(choice.machine.name)
        ranked.append(choice)

    if prefer_worker and same is not None and same.name not in busy:
        _add(SmokeMachineChoice(
            machine=same,
            is_worker=True,
            rationale=(
                f"chose {same.name} — the worker machine, idle and has "
                f"{required_caps}; its build cache is already warm"
            ),
        ))

    for m in candidates:
        if m.name == worker_machine_name or m.name in busy:
            continue
        _add(SmokeMachineChoice(
            machine=m,
            is_worker=False,
            rationale=(
                f"chose {m.name} — idle and has {required_caps} "
                f"(worker was {worker_machine_name})"
            ),
        ))

    if prefer_worker and same is not None:
        _add(SmokeMachineChoice(
            machine=same,
            is_worker=True,
            rationale=(
                f"chose {same.name} — the worker machine has {required_caps} and a "
                "warm build cache; capable but busy, smoke will queue"
            ),
        ))

    for m in candidates:
        if m.name == worker_machine_name:
            continue
        _add(SmokeMachineChoice(
            machine=m,
            is_worker=False,
            rationale=(
                f"chose {m.name} — capable but busy; smoke will queue"
            ),
        ))

    if same is not None:
        _add(SmokeMachineChoice(
            machine=same,
            is_worker=True,
            rationale=(
                f"only the worker machine ({worker_machine_name}) has {required_caps}; "
                "smoke runs on the same machine"
            ),
        ))
    return ranked


def pick_smoke_machine(
    required_caps: list[str],
    repo_name: str,
    worker_machine_name: str,
    board: Board,
    config: Config,
    *,
    prefer_worker: bool = True,
) -> SmokeMachineChoice | None:
    """The single best machine with all `required_caps` for `repo_name`.

    The head of :func:`rank_smoke_machines` — see there for the preference
    order and the reasoning. Returns None when capabilities can't be matched.

    ``dispatch_smoke`` uses the full ranking (#1672); this stays for callers
    that only ever want the first choice.
    """
    ranked = rank_smoke_machines(
        required_caps, repo_name, worker_machine_name, board, config,
        prefer_worker=prefer_worker,
    )
    return ranked[0] if ranked else None


def _capability_probe_reasons(
    machine: Machine,
    required_caps: list[str],
    *,
    http_client: httpx.Client | None = None,
    timeout: float = 5.0,
) -> dict[str, list[str]]:
    """Cross-reference `machine`'s live `/health` tool probes (#1570 B)
    against `required_caps` before routing smoke work to it (#1570 D).

    `pick_smoke_machine` only checks `machine.capabilities` — a hand-written
    claim in `coordinator.yml` that nothing has ever verified (#1570's whole
    point: `gh` was simply the first claim to bite). This asks the machine
    itself.

    Returns `{capability: [reason, ...]}` for any required capability whose
    backing tool the machine's own probe says is missing or too old — empty
    when everything checks out *or* when `/health` doesn't publish
    `tool_versions` yet (an agent that predates #1570 B). The latter fails
    OPEN, not closed: during rollout most of the fleet won't have the probe
    immediately, and refusing every smoke dispatch on missing telemetry
    would be strictly worse than the blind trust this replaces. Only an
    *explicit* probe failure refuses routing.

    Never raises — a connectivity hiccup here just skips the extra check;
    the POST to `/assign` right after this call in `dispatch_smoke` is the
    real reachability test and fails closed on its own if the machine is
    down.
    """
    client = http_client or httpx
    try:
        resp = client.get(f"http://{machine.host}:{AGENT_PORT}/health", timeout=timeout)
        resp.raise_for_status()
        health = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException, ValueError):
        # Any connectivity/parsing hiccup here just skips the extra check —
        # not widened to AttributeError: a caller's `http_client` double
        # should implement `.get` (real httpx.Client always does), so a
        # missing-method bug on our side surfaces instead of silently
        # degrading like a genuine reachability problem.
        return {}
    raw_probes = health.get("tool_versions") if isinstance(health, dict) else None
    if not raw_probes:
        return {}

    from coord.prereqs import ToolProbe, unmet_capabilities

    probes = {
        tool: ToolProbe(
            tool=tool,
            capability=info.get("capability"),
            found=bool(info.get("found", False)),
            version=info.get("version"),
            min_version=info.get("min_version"),
            meets_floor=info.get("meets_floor"),
            what_breaks="",
        )
        for tool, info in raw_probes.items()
        if isinstance(info, dict)
    }
    return unmet_capabilities(required_caps, probes)


# ── Briefing ────────────────────────────────────────────────────────────────


def build_smoke_briefing(
    *,
    repo_github: str,
    repo_name: str,
    branch: str,
    issue_number: int,
    issue_title: str,
    smoke_command: str,
    required_caps: list[str],
    timeout_seconds: int,
    is_worker: bool,
) -> str:
    lines: list[str] = []
    lines.append(f"# Smoke test: {repo_github} branch `{branch}`")
    lines.append("")
    lines.append(
        f"Validate the worker's fix for issue #{issue_number}: {issue_title}"
    )
    lines.append("")
    lines.append("## Context")
    lines.append(f"- Repo: {repo_github} (local name: {repo_name})")
    lines.append(f"- Branch: {branch}")
    if required_caps:
        lines.append(f"- Required capabilities: {', '.join(required_caps)}")
    if is_worker:
        lines.append(
            "- NOTE: only this machine has the required capabilities, so the "
            "smoke test is running on the same machine that built the change. "
            "Test the *built artifact*, not the source — the build step here is "
            "your verification that the change compiles."
        )
    lines.append(f"- Timeout: {timeout_seconds}s")
    lines.append("")
    lines.append("## What to do")
    lines.append("")
    lines.append(
        "Run these in your **worktree** (your current directory). Do NOT "
        "`cd` into the machine's shared base checkout and do NOT "
        "`git checkout` there — leaving it parked on a feature branch breaks "
        "every later dispatch against that branch on this machine (#1694)."
    )
    lines.append("")
    lines.append("```bash")
    lines.append("git fetch origin")
    lines.append(f"git checkout {branch}")
    lines.append("git pull --ff-only origin " + branch)
    lines.append(smoke_command)
    lines.append("```")
    lines.append("")
    lines.append(
        "Report `SMOKE: pass` on exit 0, or "
        "`SMOKE: fail <one-line reason>` on non-zero. The coordinator reads "
        "the final exit code."
    )
    return "\n".join(lines)


# ── Diff lookup (which files did the worker change?) ────────────────────────


def _fetch_touched_files(repo_github: str, branch: str) -> list[str]:
    """Return the list of files changed on `branch` vs the base branch.

    Uses `gh pr view --json files` so the lookup works without a local
    checkout on the coordinator. Returns an empty list on lookup failure —
    the caller treats that as "no rules matched" and skips smoke.
    """
    pr = None
    try:
        pr = github_ops.find_pr_for_branch(repo_github, branch)
    except RuntimeError:
        pr = None
    if pr is None:
        return []
    try:
        raw = github_ops._gh(
            "pr", "view", str(pr["number"]),
            "--repo", repo_github,
            "--json", "files",
        )
    except RuntimeError:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    files = data.get("files", []) or []
    return [f.get("path", "") for f in files if f.get("path")]


# ── Unroutable reporting (#1672) ────────────────────────────────────────────


#: ``test_state`` recorded on the parent work row when no capability-matched
#: machine can run the Test stage and the condition will NOT clear on its own
#: (#1672). Deliberately distinct from ``"failed"``: nothing is wrong with the
#: branch, so this must never trigger a fix round — it is a fleet/config fault,
#: and every gate that asks for ``"passed"``/``"skipped"`` keeps the merge shut
#: exactly as it did while the state was NULL.
TEST_STATE_BLOCKED = "blocked"


#: Soft (transient) unroutable reports already logged this process, keyed by
#: ``(assignment_id, message)``. Transient conditions — a machine that is
#: merely unreachable right now — are left re-dispatchable so the stage
#: self-heals on a later tick; this memo is what stops the retry from also
#: re-logging every 30 s (#1672). Bounded so a long-lived daemon can't grow it
#: without limit.
_SOFT_REPORTS_SEEN: set[tuple[str, str]] = set()
_SOFT_REPORTS_MAX = 512


@dataclass
class SmokeAttempt:
    """One machine `dispatch_smoke` tried and could not use (#1672)."""

    machine_name: str
    reason: str
    #: True when the reason is expected to clear without operator action (a
    #: connectivity blip). False for durable faults — an explicit `/health`
    #: probe contradiction (#1570 D) or a missing `repo_paths` entry — which
    #: stay broken until somebody fixes the machine or the config.
    transient: bool = False

    def describe(self) -> str:
        return f"{self.machine_name}: {self.reason}"


def _report_unroutable_smoke(
    completed: Assignment,
    required_caps: list[str],
    attempts: list[SmokeAttempt],
) -> None:
    """Report — once — that the Test stage has no machine it can run on.

    #1672/#1678: the old code logged a WARNING and returned. Nothing was
    written anywhere the TUI, `coord gates` or the board could show it, and
    the daemon re-ran the identical refusal every 30 s forever. The Test stage
    simply never started and the only trace was `journalctl` on the daemon
    host — the #1616 failure shape again: the pipeline stops and the product
    says nothing.

    Two outcomes, split on whether the condition can clear by itself:

    * **Durable** (every candidate hard-refused, or there were no candidates
      at all) — record ``test_state=TEST_STATE_BLOCKED`` with the full reason
      on the parent work row. That is board state: `coord gates` prints it,
      the TUI reads it off the row, and `record_test_verdict` writes an
      ``test_blocked`` audit row. It also ends the spin, because
      `dispatch_pending_smoke` skips rows that already carry a verdict — the
      escalation happens once, not every tick.
    * **Transient** (at least one candidate failed only on connectivity) — do
      NOT poison the row. A machine that is rebooting comes back, and marking
      the row blocked would demand a manual `coord diagnose --reset` for what
      the next tick would have fixed for free. Log it once per process
      instead, and leave the row re-dispatchable.

    Never raises: a board-write failure must not take the caller down.
    """
    transient = any(a.transient for a in attempts)
    caps = ", ".join(required_caps) if required_caps else "(none — any capable machine)"
    if attempts:
        message = (
            f"Test stage cannot be routed: every machine that declares "
            f"capability [{caps}] for repo {completed.repo_name!r} refused. "
            f"Tried {len(attempts)} — "
            + "; ".join(a.describe() for a in attempts)
            + "."
        )
    else:
        message = (
            f"Test stage cannot be routed: no configured machine declares "
            f"capability [{caps}] AND can build repo "
            f"{completed.repo_name!r} — the Test stage cannot run for this "
            f"completion until a capable machine is added."
        )

    def _log_once(level: int, text: str) -> None:
        """Log `text` at most once per (row, message) for this process.

        The board row is what normally makes the durable report fire once; a
        transient dead end deliberately does NOT write to the row (it must
        stay re-dispatchable), and a row with no assignment_id has nowhere to
        write at all — so both need this memo instead. Either way the daemon
        journal gets ONE line, never one every 30 s.
        """
        key = (completed.assignment_id or "", text)
        if key in _SOFT_REPORTS_SEEN:
            return
        if len(_SOFT_REPORTS_SEEN) >= _SOFT_REPORTS_MAX:
            _SOFT_REPORTS_SEEN.clear()
        _SOFT_REPORTS_SEEN.add(key)
        logger.log(
            level, "dispatch_smoke: %s#%s — %s",
            completed.repo_name, completed.issue_number, text,
        )

    if transient:
        _log_once(
            logging.WARNING,
            f"{message} Leaving the row re-dispatchable; a later tick "
            "retries. (#1672)",
        )
        return

    reason = (
        f"{message} Fix the machine (or add a capable one) and clear this "
        f"with `coord diagnose {completed.repo_name} "
        f"{completed.issue_number} --stage test --reset` to re-dispatch. "
        "(#1672)"
    )
    if completed.test_state == TEST_STATE_BLOCKED:
        return  # already recorded on the row — the report has been made
    if completed.assignment_id is None:
        # No row to write to (shouldn't happen for a board completion) — the
        # log is the only surface left, so at least don't repeat it forever.
        _log_once(logging.ERROR, reason)
        return
    logger.error(
        "dispatch_smoke: %s#%s — %s", completed.repo_name,
        completed.issue_number, reason,
    )
    try:
        from coord.state import record_test_verdict

        record_test_verdict(
            assignment_id=completed.assignment_id,
            test_state=TEST_STATE_BLOCKED,
            test_reason=reason,
        )
    except Exception:  # noqa: BLE001 — reporting must never break dispatch
        logger.exception(
            "dispatch_smoke: failed to record the blocked Test verdict for %s",
            completed.assignment_id,
        )
        return
    completed.test_state = TEST_STATE_BLOCKED
    completed.test_reason = reason


# ── Dispatch ────────────────────────────────────────────────────────────────


PRLookup = Callable[..., dict | None]
DiffLookup = Callable[[str, str], list[str]]


def dispatch_smoke(
    completed: Assignment,
    board: Board,
    config: Config,
    *,
    http_client: httpx.Client | None = None,
    diff_lookup: DiffLookup = _fetch_touched_files,
    now: float | None = None,
) -> Assignment | None:
    """Queue a smoke test for a completed work-like assignment (#930: also
    ``type="mock-author"`` — see :data:`coord.models.WORK_LIKE_TYPES`).

    Returns the new smoke `Assignment`, or None when no smoke is needed
    (no rules matched, no capable machine, smoke disabled, etc.). The
    caller is responsible for persisting the board.

    #1672: routing walks the FULL capability-matched candidate list (see
    :func:`rank_smoke_machines`) instead of standing or falling on one
    machine, and a dead end is reported on the row rather than re-logged on
    every daemon tick — see :func:`_report_unroutable_smoke`.
    """
    smoke_cfg = getattr(config, "smoke_tests", SmokeTestsConfig())
    if not smoke_cfg.auto_queue:
        return None
    if completed.type not in WORK_LIKE_TYPES:
        return None
    if completed.status != "done":
        return None
    if not completed.branch:
        return None
    if completed.test_state == TEST_STATE_BLOCKED:
        # #1672: already reported as unroutable, with the reason on the row.
        # Re-probing the same broken fleet on every tick is exactly the spin
        # this issue is about — an operator clears it (`coord diagnose
        # --stage test --reset`) once the fleet is fixed. `dispatch_pending_
        # smoke` already skips rows with a verdict; this covers the callers
        # that hand us a row directly (reconcile).
        return None

    # Dedupe: don't fire a second smoke if one's already in flight.
    from coord.claim import (
        has_active_branch_followup,
        has_active_followup,
        superseding_work_row,
    )

    if has_active_followup(
        board, of_assignment_id=completed.assignment_id, assignment_type="smoke"
    ):
        return None

    # #1819: ...and don't fire one if another row on the SAME BRANCH already
    # has one in flight. The suite measures the branch, not the row that
    # pushed it; after a fix round (`--fix-of` reuses the branch by design)
    # one branch carries two `work` rows, and the row-keyed dedupe above
    # waved the sibling straight through — two machines ran the identical
    # suite on the identical branch and raced to write the verdict (#1797).
    if has_active_branch_followup(
        board,
        repo_name=completed.repo_name,
        branch=completed.branch,
        assignment_type="smoke",
    ):
        return None

    # #1819: a row that a LATER work-like row superseded on the same branch is
    # not a dispatch target at all. It did not produce the branch's current
    # content, so testing it burns a machine on a result the later row's own
    # dispatch already computes, and the verdict lands on a row nothing gates
    # on. This is what keeps the round-1 row (review=request-changes, fixed by
    # round 2) from consuming a machine every time the base moves.
    superseded_by = superseding_work_row(board, completed)
    if superseded_by is not None:
        logger.debug(
            "dispatch_smoke: skipping %s#%s row %s — superseded on branch %s "
            "by the later work row %s (#1819).",
            completed.repo_name, completed.issue_number,
            completed.assignment_id, completed.branch,
            getattr(superseded_by, "assignment_id", None),
        )
        return None

    repo = config.repo(completed.repo_name)
    if repo is None:
        return None

    touched = diff_lookup(repo.github, completed.branch)
    required_caps = match_rules(touched, smoke_cfg.capability_rules)
    smoke_command = smoke_cfg.default_command or repo.test_command

    if not required_caps:
        # #1426: a capability-rule miss used to mean "skip silently" — the
        # exact blocker that kept the Test stage from ever dispatching for
        # any repo/diff not explicitly covered by a `capability_rules` entry
        # (historically only `tui/` and `coord/dashboard/webapp/` were
        # routed; everything else — most `coord/**` Python work included —
        # never got a headless Test-stage dispatch at all).
        #
        # It now means "no EXTRA hardware capability required", not "nothing
        # to test": a `type="work"` completion still dispatches, to any
        # machine that can build/test the repo at all, as long as a real
        # command is configured (`smoke_tests.default_command` or the repo's
        # `test_command`) — see `pick_smoke_machine`, which treats an empty
        # `required_caps` as "any capable-for-repo machine".
        #
        # `mock-author`/`test-author` (#930/#1176) keep the OLD skip-on-miss
        # behavior: #1076/#1152 established that a rule miss for THOSE types
        # means "genuinely nothing to smoke-test" (a Gate-A contract/fixture-
        # only diff), and `dispatch_pending_reviews` back-fills
        # `test_state="skipped"` for them — dispatching a real suite run
        # here would duplicate that and burn a full test run on a diff that
        # never touches source.
        if completed.type != "work" or smoke_command is None:
            return None

    if smoke_command is None:
        logger.warning(
            "dispatch_smoke: %s#%s needs capabilities %s but no smoke "
            "command is configured (smoke_tests.default_command or this "
            "repo's test_command) — skipping. Configure one so the Test "
            "stage stops silently no-oping for this repo.",
            completed.repo_name, completed.issue_number, required_caps,
        )
        return None

    # #1672: the FULL capability-matched candidate list, best first. Picking
    # one machine and giving up on it meant a single bad candidate ended the
    # whole Test stage — #1678, where the router re-chose the same unhealthy
    # machine every 30 s while two other machines declared the same
    # capability and were never tried. Capability matching itself is NOT
    # relaxed: `rank_smoke_machines` only ever yields machines that genuinely
    # declare every required capability.
    candidates = rank_smoke_machines(
        required_caps, completed.repo_name, completed.machine_name, board, config
    )
    attempts: list[SmokeAttempt] = []
    client = http_client or httpx
    dispatched: tuple[SmokeMachineChoice, str, dict] | None = None

    for choice in candidates:
        if required_caps:
            unmet = _capability_probe_reasons(
                choice.machine, required_caps, http_client=http_client
            )
            if unmet:
                # #1570 D: the machine *claims* every required capability in
                # `coordinator.yml`, but its own `/health` probe (#1570 B)
                # says otherwise — refuse to route HERE rather than dispatch
                # a worker that fails 20 minutes in with a confusing,
                # unrelated error. #1672: that refusal is per-machine, so
                # keep walking the candidate list instead of ending the
                # stage. Durable, not transient — the probe disagrees until
                # somebody installs the tool.
                logger.warning(
                    "dispatch_smoke: skipping machine %s for %s#%s — its own "
                    "/health probe disagrees with its declared capabilities "
                    "%s — %s — refusing to route (#1570 D). Trying the next "
                    "capability-matched machine (#1672); run `coord doctor` "
                    "to check the fleet.",
                    choice.machine.name, completed.repo_name,
                    completed.issue_number, required_caps, unmet,
                )
                attempts.append(SmokeAttempt(
                    machine_name=choice.machine.name,
                    reason=(
                        f"/health probe contradicts its declared capabilities "
                        f"{required_caps} — {unmet} (#1570 D)"
                    ),
                ))
                continue

        repo_path = choice.machine.repo_path(completed.repo_name)
        if repo_path is None:
            logger.warning(
                "dispatch_smoke: skipping machine %s for %s#%s — it has no "
                "repo_paths entry for %r.",
                choice.machine.name, completed.repo_name,
                completed.issue_number, completed.repo_name,
            )
            attempts.append(SmokeAttempt(
                machine_name=choice.machine.name,
                reason=f"no repo_paths entry for {completed.repo_name!r}",
            ))
            continue

        briefing = build_smoke_briefing(
            repo_github=repo.github,
            repo_name=repo.name,
            branch=completed.branch,
            issue_number=completed.issue_number,
            issue_title=completed.issue_title,
            smoke_command=smoke_command,
            required_caps=required_caps,
            timeout_seconds=smoke_cfg.timeout_seconds,
            is_worker=choice.is_worker,
        )

        payload = {
            "repo_name": completed.repo_name,
            "repo_path": repo_path,
            "issue_number": completed.issue_number,
            "issue_title": f"[smoke] {completed.issue_title}",
            "briefing": briefing,
            "files_allowed": [],
            "files_forbidden": [],
            "pull_repos": [],
            "type": "smoke",
            "system_prompt": SMOKE_SYSTEM_PROMPT,
            "review_target": completed.branch,
            # #255: smoke checks out the worker's PR branch but the agent still
            # consults `branch` as the integration base.
            "branch": repo.default_branch or "main",
        }

        url = f"http://{choice.machine.host}:{AGENT_PORT}/assign"
        try:
            resp = client.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            agent_response = resp.json()
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            # #1672: TRANSIENT — the machine is capable and its probe agreed,
            # it just didn't answer. Try the next candidate, but if none is
            # left the row stays re-dispatchable rather than blocked: a
            # rebooting machine comes back, and poisoning the row would cost
            # an operator a manual reset for something the next tick fixes.
            logger.warning(
                "dispatch_smoke: POST /assign to %s for %s#%s failed (%s) — "
                "trying the next capability-matched machine (#1672).",
                choice.machine.name, completed.repo_name,
                completed.issue_number, exc,
            )
            attempts.append(SmokeAttempt(
                machine_name=choice.machine.name,
                reason=f"POST /assign failed — {exc}",
                transient=True,
            ))
            continue

        dispatched = (choice, briefing, agent_response)
        break

    if dispatched is None:
        # Every capability-matched machine was tried and none could take it
        # (or there were none at all). Report it where the board can show it,
        # exactly once — never the silent 30 s spin of #1678.
        _report_unroutable_smoke(completed, required_caps, attempts)
        return None

    choice, briefing, agent_response = dispatched

    smoke_assignment = Assignment(
        machine_name=choice.machine.name,
        repo_name=completed.repo_name,
        issue_number=completed.issue_number,
        issue_title=f"[smoke] {completed.issue_title}",
        files_allowed=[],
        files_forbidden=[],
        briefing=briefing,
        assignment_id=agent_response.get("id") or uuid.uuid4().hex[:12],
        status="running",
        branch=completed.branch,
        pr_url=completed.pr_url,
        dispatched_at=now if now is not None else time.time(),
        type="smoke",
        review_target=completed.branch,
        review_of_assignment_id=completed.assignment_id,
    )
    board.active.append(smoke_assignment)

    from coord.state import record_dispatched_assignment
    repo = config.repo(completed.repo_name)
    if repo is not None:
        record_dispatched_assignment(
            assignment=smoke_assignment,
            repo_github=repo.github,
        )

    # #1395/#1426: mark the PARENT work row's Test verdict "running" the
    # moment the smoke assignment is dispatched — the same marker
    # `coord test --running` set for the old local-subprocess path, so the
    # board/TUI reads the Test box Active for the run's duration instead of
    # idle/Pending. Without this, dispatching the Test stage as a real
    # assignment would silently reopen the #1395 gap it was built to close:
    # `test_state` would stay NULL from dispatch until the terminal verdict
    # lands, indistinguishable from "not started yet".
    #
    # #1819: ...but NEVER over a terminal verdict. `running` is read as "no
    # verdict yet" by every gate (#1395), so stamping it on a row that already
    # says `passed`/`skipped` *un-satisfies a gate that was satisfied* — the
    # merge entry drops out of the queue and the whole cycle restarts. That is
    # the self-sustaining loop observed on #1797: verdict lands → merge
    # enqueues → a re-dispatch a minute later clobbers the gate field back to
    # `running` → the merge never fires → repeat. Dispatching a *fresh* run
    # must never, by itself, retract the previous answer; the new verdict
    # replaces the old one when it actually lands.
    if completed.assignment_id is not None and completed.test_state not in (
        "passed", "skipped", "failed",
    ):
        from coord.state import record_test_verdict

        record_test_verdict(
            assignment_id=completed.assignment_id,
            test_state="running",
            test_reason="dispatched: Test stage running (#1426)",
        )
        completed.test_state = "running"

    return smoke_assignment


# ── Bulk dispatch (#1426) ────────────────────────────────────────────────────


def dispatch_pending_smoke(
    board: Board,
    config: Config,
    *,
    now: float | None = None,
) -> list[Assignment]:
    """Bulk Test-stage dispatch — the smoke analogue of
    :func:`coord.review.dispatch_pending_reviews`.

    Scans the FULL completed backlog on `board` (not just rows that just
    transitioned this pass) for work-like completions with no test verdict
    yet, and dispatches a smoke assignment for each eligible one via
    :func:`dispatch_smoke` (which itself enforces `auto_queue`, the #459-style
    dedupe via `has_active_followup`, and capability routing).

    This is the single choke point both `reconcile()` (human-invoked `coord
    resume`) and `coord notify` (the unattended 5-minute timer) route bulk
    Test-stage dispatch through — mirroring `dispatch_pending_reviews`
    exactly. Before this, `dispatch_smoke` was only ever called from
    `reconcile()`'s per-item loop over that pass's newly-done rows, so a
    thin-client/timer-only setup with nobody running `coord resume` never
    dispatched the Test stage at all — the gap `drive-issue.sh` had to paper
    over with a local `scripts/coord-test-runner.sh` subprocess (#1395).

    Returns the list of smoke `Assignment`s actually dispatched. The caller
    is responsible for persisting the board.
    """
    smoke_cfg = getattr(config, "smoke_tests", None)
    if smoke_cfg is None or not smoke_cfg.auto_queue:
        return []

    from coord.state import get_issue_test_mode

    dispatched: list[Assignment] = []
    for completed in board.completed:
        if completed.type not in WORK_LIKE_TYPES:
            continue
        if completed.status != "done":
            continue
        if completed.test_state is not None:
            # Already has a verdict ("passed"/"failed"/"skipped"), or is
            # "running" — someone (an interactive --smoke-of session, or a
            # smoke assignment already in flight) is already handling it.
            #
            # #1672: this is also what makes the unroutable report fire ONCE.
            # `dispatch_smoke` records `test_state="blocked"` when no
            # capability-matched machine can take the stage, so the next tick
            # lands here and skips instead of re-probing a fleet that is
            # still broken and re-logging the identical refusal every 30 s
            # (#1678). Clearing it (`coord diagnose <repo> <issue> --stage
            # test --reset`) puts the row back in this scan.
            continue

        # #685: per-issue test-mode policy gates auto-smoke dispatch.
        #   test-mode:auto  → headless smoke (auto-dispatch here).
        #   test-mode:smoke → skip; the TUI offers the interactive smoke agent.
        #   no label        → no policy set → respect auto_queue (back-compat).
        test_mode = get_issue_test_mode(completed.repo_name, completed.issue_number)
        if test_mode == "smoke":
            continue

        smoke = dispatch_smoke(completed, board, config, now=now)
        if smoke is not None:
            dispatched.append(smoke)

    return dispatched
