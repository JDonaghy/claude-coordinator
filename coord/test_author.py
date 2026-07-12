"""Dispatch a `type="test-author"` session (#931, docs/ORACLE_LOOP.md).

An independent, feature-level black-box acceptance-suite author — zero
shared context with the worker under test, the same independence principle
as the adversarial reviewer (`coord/review.py`). Authored **at the Gate-A
arch gate, before any work**, then extended **just-in-time** as each issue
firms up its slice of the surface.

Like `coord/smoke.py` / `coord/review.py` / `coord/conflict_fix.py`, this
bypasses `coord.dispatch.dispatch()` (the `Proposal`/brain-oriented path)
and POSTs directly to the agent's `/assign` endpoint. That matters here
specifically: `coord.dispatch.dispatch()` auto-forbids `tests/acceptance/`
for every proposal when the repo has an acceptance driver configured (the
seal that keeps a `type="work"` worker from editing the oracle it's
graded against) — but a test-author session's entire job IS writing there.
Bypassing `dispatch()` means that seal never applies to this type, by
construction, instead of needing a type-gated exception in a shared
hot path.

The dispatcher does NOT read `contract.md` itself — no local checkout is
required to dispatch. The worker gets a full worktree (this is a
write-capable, non-chat type, so `AgentServer.assign` gives it one same as
`work`/`smoke`/`conflict-fix`) and is told, in its briefing, to read the
contract from its own checkout. This mirrors `coord.acceptance.
oracle_loop_contract_block` (#945), which points the *worker's* briefing at
the same path rather than embedding the contract text.
"""

from __future__ import annotations

import time
import uuid

import httpx

from coord import github_ops
from coord.acceptance import ACCEPTANCE_DIRNAME
from coord.config import Config
from coord.dispatch import AGENT_PORT
from coord.machine_pause import paused_set
from coord.milestone_dispatch import MilestoneDispatchError, fetch_milestone_context
from coord.models import Assignment, Machine

# The test-author never needs `gh` — every fact it needs (tracking issue,
# milestone membership, the JIT issue's title/body) is fetched by the
# coordinator and embedded in the briefing before dispatch. Mirrors
# `conflict_fix.CONFLICT_FIX_DENY_COMMANDS`'s "the coordinator owns GitHub
# interactions" rationale.
TEST_AUTHOR_DENY_COMMANDS: list[str] = [
    "Bash(gh *)",
]

TEST_AUTHOR_SYSTEM_PROMPT = """\
You are an INDEPENDENT acceptance-test author dispatched by the coordinator.

You have ZERO shared context with whoever implements this milestone's issues \
— the same independence principle as an adversarial code reviewer. You write \
to the CONTRACT, never to an implementation. Do not read, diff, or reason \
about any work branch, PR, or commit for the issue(s) in your briefing — if \
one exists, ignore it. Your tests must be derivable from the contract and \
the issue description alone. If you find yourself wanting to peek at code to \
"see what it actually does," stop — that would make you the worker grading \
its own homework, which is exactly what this assignment type exists to \
prevent.

Your job:
1. Read the contract at the path given in your briefing \
(`tests/acceptance/ms-NN/contract.md`) from YOUR OWN checkout. If it does \
not exist, STOP and output:
     STUCK: contract.md missing at <path> — Gate-A hasn't produced it yet
   Do not invent a contract yourself.
2. Author (or extend) the acceptance suite in `tests/acceptance/ms-NN/`, \
using the repo's declared driver framework (kind + run command are in your \
briefing) — the tests must be runnable by that exact command.
3. Update `tests/acceptance/ms-NN/manifest.(yml|json)` mapping every test id \
you added/kept to its issue number, in either accepted shape:
     tests: {<test-id>: <issue-number>, ...}
   or
     issues: {<issue-number>: [<test-id>, ...], ...}
   Merge with the existing manifest rather than clobbering it — other \
issues' slices may already be authored.
4. Your tests MUST be RED right now (the implementation doesn't exist yet). \
Run the driver's run command yourself and confirm the new/changed tests \
fail (not error out from a missing framework hookup) — a red suite that \
doesn't even execute is not useful to the worker who inherits it.
5. Do NOT touch anything outside `tests/acceptance/ms-NN/**` — you are not \
implementing, refactoring, or fixing anything else in the repo.
6. Commit and push your branch. Do not open a PR — the coordinator handles \
that.

If the contract is ambiguous or silent on a case you think matters, don't \
guess: write the test with a `// TODO(test-author): contract doesn't specify
X` comment (or the language's equivalent) rather than inventing behavior, \
and call it out in your final summary.
"""


def pick_test_author_machine(
    config: Config, repo_name: str, required_capability: str = ""
) -> Machine | None:
    """Pick a machine to run the test-author session on.

    Any qualified, unpaused machine that has the repo cloned and (when the
    repo's driver declares one) the required capability — mirrors
    `refine_chat.pick_refinement_machine`'s "any qualified machine works"
    simplicity; test-author is a one-off CLI-triggered dispatch (Gate A /
    JIT), not a high-frequency auto-dispatch, so no idle/busy weighting is
    needed. Unlike smoke, there's no "different from the worker" axis here
    — there is no single worker machine to avoid, by design.
    """
    paused = paused_set()
    for m in config.machines:
        if not m.can_work_on(repo_name):
            continue
        if m.repo_path(repo_name) is None:
            continue
        if m.name in paused:
            continue
        if required_capability and required_capability not in m.capabilities:
            continue
        return m
    return None


def build_test_author_briefing(
    *,
    repo_name: str,
    repo_github: str,
    ms_dir: str,
    tracking_issue: int,
    milestone_number: int,
    milestone_issue_numbers: list[int],
    driver_kind: str,
    driver_run: str,
    issue_number: int | None,
    issue_title: str | None,
    issue_body: str | None,
) -> str:
    """Compose the test-author's briefing (its first/only user message).

    Two modes, matching docs/ORACLE_LOOP.md's "authored red ... before the
    work, then extended just-in-time": *milestone mode* (`issue_number` is
    None) authors the full initial suite from the contract; *JIT mode*
    (`issue_number` set) extends just that issue's slice.
    """
    contract_path = f"{ACCEPTANCE_DIRNAME}/{ms_dir}/contract.md"
    manifest_glob = f"{ACCEPTANCE_DIRNAME}/{ms_dir}/manifest.(yml|json)"

    parts: list[str] = []
    parts.append(
        f"=== Independent test-author session for {repo_github} — "
        f"milestone #{milestone_number} (tracking issue #{tracking_issue}) ===\n"
    )
    parts.append(f"CONTRACT: {contract_path}")
    parts.append(f"MANIFEST: {manifest_glob}")
    parts.append(f"DRIVER: kind={driver_kind!r}  run={driver_run!r}")
    parts.append(
        f"MILESTONE WORK-ORDER ISSUES: {milestone_issue_numbers or '(none recorded yet)'}"
    )
    parts.append("")

    if issue_number is None:
        parts.append(
            "MODE: full milestone authoring (Gate A). Author the initial red "
            f"acceptance suite under `{ACCEPTANCE_DIRNAME}/{ms_dir}/` covering "
            "the whole black-box surface in the contract, with at least one "
            "test per issue in the work-order list above, and write the "
            "manifest mapping every test id to its issue number."
        )
    else:
        parts.append(
            f"MODE: just-in-time slice extension for issue #{issue_number}. "
            "Extend the existing suite with tests covering ONLY this issue's "
            "slice of the black-box surface — leave other issues' tests as "
            "they are, and merge (don't clobber) the manifest."
        )
        parts.append("")
        parts.append(f"ISSUE #{issue_number}: {issue_title or '(no title)'}")
        parts.append(issue_body.strip() if issue_body and issue_body.strip() else "(no body)")

    parts.append("")
    parts.append("---")
    parts.append(
        "Follow the steps in your system prompt (read contract → author/"
        "extend → update manifest → verify red → commit + push, no PR)."
    )
    return "\n".join(parts)


def dispatch_test_author(
    repo_name: str,
    tracking_issue: int,
    config: Config,
    *,
    issue_number: int | None = None,
    machine_override: str | None = None,
    http_client: httpx.Client | None = None,
) -> tuple[str, str]:
    """End-to-end: resolve the milestone, pick a machine, seed the
    briefing, dispatch a `type="test-author"` assignment.

    Returns `(assignment_id, machine_name)`. Raises `RuntimeError` on any
    resolution failure (unknown repo, no acceptance driver configured, bad
    tracking issue, `issue_number` not a member of the milestone's work
    order, no qualified machine, or the agent rejecting the dispatch).
    """
    repo_cfg = config.repo(repo_name)
    if repo_cfg is None:
        raise RuntimeError(f"repo {repo_name!r} not in coordinator.yml")

    driver_cfg = config.acceptance.driver_for(repo_name)
    if driver_cfg is None:
        raise RuntimeError(
            f"no acceptance driver configured for repo {repo_name!r} "
            "(add it under acceptance.drivers in coordinator.yml)"
        )

    try:
        ctx = fetch_milestone_context(repo_cfg, tracking_issue)
    except MilestoneDispatchError as e:
        raise RuntimeError(str(e)) from e

    if issue_number is not None and ctx.work_order.node(issue_number) is None:
        raise RuntimeError(
            f"issue #{issue_number} is not a member of milestone "
            f"#{ctx.milestone_number}'s work order (tracking issue #{tracking_issue})"
        )

    if machine_override:
        machine = next(
            (m for m in config.machines if m.name == machine_override), None
        )
        if machine is None:
            raise RuntimeError(f"machine {machine_override!r} not in coordinator.yml")
        if not machine.can_work_on(repo_name):
            raise RuntimeError(
                f"machine {machine_override!r} does not list repo {repo_name!r}"
            )
    else:
        machine = pick_test_author_machine(config, repo_name, driver_cfg.capability)
        if machine is None:
            cap_note = (
                f" with capability {driver_cfg.capability!r}"
                if driver_cfg.capability else ""
            )
            raise RuntimeError(
                f"no machine claims repo {repo_name!r}{cap_note} — "
                "test-author needs a machine with the repo cloned"
                + (" and the driver's required capability" if cap_note else "")
            )

    repo_path = machine.repo_path(repo_name)
    if repo_path is None:
        raise RuntimeError(
            f"machine {machine.name!r} has no repo_path for {repo_name!r}"
        )

    issue_title: str | None = None
    issue_body: str | None = None
    if issue_number is not None:
        try:
            issue_data = github_ops.get_issue(repo_cfg.github, issue_number)
        except RuntimeError as e:
            raise RuntimeError(f"could not fetch #{issue_number}: {e}") from e
        issue_title = issue_data.get("title") or ""
        issue_body = issue_data.get("body") or ""

    ms_dir = f"ms-{ctx.milestone_number}"
    briefing = build_test_author_briefing(
        repo_name=repo_name,
        repo_github=repo_cfg.github,
        ms_dir=ms_dir,
        tracking_issue=tracking_issue,
        milestone_number=ctx.milestone_number,
        milestone_issue_numbers=list(ctx.work_order.issue_numbers),
        driver_kind=driver_cfg.kind,
        driver_run=driver_cfg.run,
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
    )

    # Fixed assignment title regardless of mode (milestone vs. JIT slice) so
    # repeated dispatches for the same milestone derive the SAME branch name
    # (issue-{tracking_issue}-{slug(title)}, see AgentServer._setup_worktree)
    # — JIT extensions continue the same branch/PR instead of forking a new
    # one each time.
    assignment_title = f"[test-author] ms-{ctx.milestone_number} acceptance suite"

    repo_deny = repo_cfg.worker_permissions.deny if repo_cfg.worker_permissions else []
    deny_commands = list(dict.fromkeys(list(repo_deny) + TEST_AUTHOR_DENY_COMMANDS))

    payload = {
        "repo_name": repo_name,
        "repo_path": repo_path,
        "issue_number": tracking_issue,
        "issue_title": assignment_title,
        "briefing": briefing,
        "files_allowed": [],
        "files_forbidden": [],
        "pull_repos": [],
        "type": "test-author",
        "system_prompt": TEST_AUTHOR_SYSTEM_PROMPT,
        "deny_commands": deny_commands,
        "branch": repo_cfg.default_branch or "main",
    }

    url = f"http://{machine.host}:{AGENT_PORT}/assign"
    client = http_client or httpx
    resp = client.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    agent_response = resp.json()

    assignment_id = agent_response.get("id") or uuid.uuid4().hex[:12]

    asg = Assignment(
        machine_name=machine.name,
        repo_name=repo_name,
        issue_number=tracking_issue,
        issue_title=assignment_title,
        files_allowed=[],
        files_forbidden=[],
        briefing=briefing,
        assignment_id=assignment_id,
        status="running",
        dispatched_at=time.time(),
        type="test-author",
        # #1084: correlate this JIT dispatch back to the specific member
        # issue it's extending, so the TUI's per-issue Acceptance-Authoring
        # mini-pipeline can tell "issue #1039's slice" apart from a sibling
        # issue's slice sharing the same tracking-issue-keyed assignment
        # row. None in milestone mode (issue_number is None) — Gate A's own
        # mock-author track doesn't need this field.
        for_issue_number=issue_number,
    )

    from coord.state import record_dispatched_assignment  # noqa: PLC0415

    record_dispatched_assignment(assignment=asg, repo_github=repo_cfg.github)

    return assignment_id, machine.name
