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
- `pick_smoke_machine(required_caps, worker_machine, board, config)` — picks
  a capable machine, preferring the worker's own (its build cache is warm —
  #1402); pass `prefer_worker=False` for the old different-machine-first
  order.
- `dispatch_smoke(completed, board, config, ...)` — the full path; called
  from reconcile when a work assignment transitions to done.

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

Steps:
1. `git fetch origin && git checkout <branch>` (the branch is in your briefing).
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


def pick_smoke_machine(
    required_caps: list[str],
    repo_name: str,
    worker_machine_name: str,
    board: Board,
    config: Config,
    *,
    prefer_worker: bool = True,
) -> SmokeMachineChoice | None:
    """Pick a machine with all `required_caps` for `repo_name`.

    Preference order (#1402):
    1. The worker's own machine, if capable and idle
    2. Idle, capable, different from worker
    3. The worker's own machine, if capable (busy — smoke will queue)
    4. Busy, capable, different from worker (smoke will queue)
    5. None — no machine can validate this change

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

    Returns None when capabilities can't be matched.
    """
    candidates = [
        m for m in config.machines
        if m.can_work_on(repo_name)
        and all(cap in m.capabilities for cap in required_caps)
    ]
    if not candidates:
        return None

    busy = {a.machine_name for a in board.active if a.status in ("pending", "running")}

    same = next((m for m in candidates if m.name == worker_machine_name), None)

    if prefer_worker and same is not None and same.name not in busy:
        return SmokeMachineChoice(
            machine=same,
            is_worker=True,
            rationale=(
                f"chose {same.name} — the worker machine, idle and has "
                f"{required_caps}; its build cache is already warm"
            ),
        )

    idle_different = [
        m for m in candidates
        if m.name != worker_machine_name and m.name not in busy
    ]
    if idle_different:
        return SmokeMachineChoice(
            machine=idle_different[0],
            is_worker=False,
            rationale=(
                f"chose {idle_different[0].name} — idle and has {required_caps} "
                f"(worker was {worker_machine_name})"
            ),
        )

    if prefer_worker and same is not None:
        return SmokeMachineChoice(
            machine=same,
            is_worker=True,
            rationale=(
                f"chose {same.name} — the worker machine has {required_caps} and a "
                "warm build cache; capable but busy, smoke will queue"
            ),
        )

    busy_different = [
        m for m in candidates if m.name != worker_machine_name
    ]
    if busy_different:
        return SmokeMachineChoice(
            machine=busy_different[0],
            is_worker=False,
            rationale=(
                f"chose {busy_different[0].name} — capable but busy; smoke will queue"
            ),
        )

    if same is not None:
        return SmokeMachineChoice(
            machine=same,
            is_worker=True,
            rationale=(
                f"only the worker machine ({worker_machine_name}) has {required_caps}; "
                "smoke runs on the same machine"
            ),
        )
    return None


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

    # Dedupe: don't fire a second smoke if one's already in flight.
    from coord.claim import has_active_followup

    if has_active_followup(
        board, of_assignment_id=completed.assignment_id, assignment_type="smoke"
    ):
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

    choice = pick_smoke_machine(
        required_caps, completed.repo_name, completed.machine_name, board, config
    )
    if choice is None:
        logger.warning(
            "dispatch_smoke: %s#%s needs capabilities %s but no configured "
            "machine has them (and can build repo %r) — the Test stage will "
            "not run for this completion until a capable machine is added.",
            completed.repo_name, completed.issue_number, required_caps,
            completed.repo_name,
        )
        return None

    if required_caps:
        unmet = _capability_probe_reasons(
            choice.machine, required_caps, http_client=http_client
        )
        if unmet:
            # #1570 D: the machine *claims* every required capability in
            # `coordinator.yml`, but its own `/health` probe (#1570 B) says
            # otherwise — refuse to route here rather than dispatch a worker
            # that fails 20 minutes in with a confusing, unrelated error.
            logger.warning(
                "dispatch_smoke: chose machine %s for %s#%s but its own "
                "/health probe disagrees with its declared capabilities "
                "%s — %s — refusing to route (#1570 D). Run `coord doctor` "
                "to check the fleet.",
                choice.machine.name, completed.repo_name,
                completed.issue_number, required_caps, unmet,
            )
            return None

    repo_path = choice.machine.repo_path(completed.repo_name)
    if repo_path is None:
        logger.warning(
            "dispatch_smoke: chose machine %s for %s#%s but it has no "
            "repo_paths entry for %r — cannot dispatch.",
            choice.machine.name, completed.repo_name, completed.issue_number,
            completed.repo_name,
        )
        return None

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
    client = http_client or httpx
    try:
        resp = client.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        agent_response = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        return None

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
    if completed.assignment_id is not None:
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
