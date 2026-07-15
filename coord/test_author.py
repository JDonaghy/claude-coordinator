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

import os
import sys
import time
import uuid
from pathlib import Path

import click
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

# #1173: the human-attended (`coord acceptance author --interactive`) variant
# of the system prompt above. The independence contract is UNCHANGED — same
# rules, same STOP-on-missing-contract, same "don't peek at the work branch"
# — this only tells the model an operator is attached, since that's a
# material fact about the session it would otherwise have no way to know.
TEST_AUTHOR_INTERACTIVE_SYSTEM_PROMPT = (
    TEST_AUTHOR_SYSTEM_PROMPT
    + "\n\nThis session is HUMAN-ATTENDED: an operator is at the keyboard "
    "with you, watching your output and able to redirect you. That does "
    "NOT relax anything above — you still author from the contract alone, "
    "with zero shared context with the implementation. The operator is "
    "here to catch a bad contract/mock read, a stuck session, or an "
    "ambiguous case worth discussing out loud — not to hand you "
    "implementation details or steer you toward a specific test shape "
    "beyond what the contract says.\n"
)


def test_author_deny_commands(config: Config, repo_name: str) -> list[str]:
    """Merge the repo's configured deny-list with :data:`TEST_AUTHOR_DENY_COMMANDS`.

    Shared by :func:`dispatch_test_author` (the original dispatch) and
    ``coord.auto_loop._dispatch_fix`` (a bounced test-author fix, #1176) so
    the two call sites can't drift — a fix session POSTs `type="test-author"`
    directly to `/assign` the same way the original dispatch does, and needs
    the identical guardrails.
    """
    repo_cfg = config.repo(repo_name)
    repo_deny = (
        repo_cfg.worker_permissions.deny
        if repo_cfg and repo_cfg.worker_permissions
        else []
    )
    return list(dict.fromkeys(list(repo_deny) + TEST_AUTHOR_DENY_COMMANDS))


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
    path: str | None = None,
    http_client: httpx.Client | None = None,
) -> tuple[str, str]:
    """End-to-end: resolve the milestone, pick a machine, seed the
    briefing, dispatch a `type="test-author"` assignment.

    *path* (#1125, repo-root-relative, e.g. ``"coord/foo.py"``) resolves
    which driver to use when the repo's acceptance config is routed
    (``acceptance.drivers.<repo>.routes``) — pass the milestone/issue's
    representative subtree (see `AcceptanceConfig.driver_for` for the
    single-path-per-call resolution rule). Unused (and unneeded) when the
    repo has a flat, unrouted driver.

    Returns `(assignment_id, machine_name)`. Raises `RuntimeError` on any
    resolution failure (unknown repo, no acceptance driver configured (or,
    for a routed repo, no driver resolves for *path*), bad tracking issue,
    `issue_number` not a member of the milestone's work order, no qualified
    machine, or the agent rejecting the dispatch).

    Branch: milestone mode (`issue_number=None`) shares one branch/PR across
    repeated calls, keyed on *tracking_issue*. JIT mode (`issue_number` set)
    gets its own per-slice branch keyed on `(tracking_issue, issue_number)`
    — required so each member issue's slice can merge independently without
    stranding the next slice on an already-closed PR (#1171).
    """
    repo_cfg = config.repo(repo_name)
    if repo_cfg is None:
        raise RuntimeError(f"repo {repo_name!r} not in coordinator.yml")

    driver_cfg = config.acceptance.driver_for(repo_name, path)
    if driver_cfg is None:
        if config.acceptance.has_driver(repo_name):
            raise RuntimeError(
                f"repo {repo_name!r} has a routed acceptance driver "
                "(acceptance.drivers routes) but no route matched — pass "
                "--for-path to select the milestone's subtree (e.g. "
                "'coord/**')"
            )
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

    # #1171: milestone-mode dispatches (issue_number is None) keep a single
    # FIXED assignment title so repeated calls derive the SAME shared branch
    # (issue-{tracking_issue}-{slug(title)}, see AgentServer._setup_worktree)
    # — that's Gate A's "extend the same in-flight suite" case, and the
    # branch's PR does not merge until the whole milestone's suite is ready.
    #
    # JIT slices (issue_number set) must NOT reuse that shared branch: each
    # member issue's slice needs its own PR because #1138's oracle gate
    # merges it to unblock that issue's own Work — so the shared branch's PR
    # is guaranteed closed before the next slice is authored, silently
    # stranding it (#1171). Key the branch on (milestone, member issue)
    # instead via an explicit `target_branch`, deliberately OUTSIDE the
    # `issue-{N}-*` namespace: if this PR is squash-merged (ancestry breaks)
    # and deleteBranchOnMerge=false lets the branch survive, an
    # `issue-{issue_number}-*` name would false-positive `coord.claim`'s
    # remote-branch check for that same member issue's own Work dispatch —
    # re-wedging the exact stall this fix removes. A retry/continuation for
    # the SAME (tracking_issue, issue_number) pair still resolves to the
    # same branch name, so extending an already-authored slice keeps
    # pushing to its own still-open PR rather than forking a new one.
    if issue_number is None:
        assignment_title = f"[test-author] ms-{ctx.milestone_number} acceptance suite"
        target_branch: str | None = None
    else:
        assignment_title = (
            f"[test-author] ms-{ctx.milestone_number} slice #{issue_number}"
        )
        target_branch = f"test-author-ms-{ctx.milestone_number}-slice-{issue_number}"

    # #1172: resolve the branch this dispatch would push onto UP FRONT (same
    # formula `AgentServer._setup_worktree` / the post-dispatch recording
    # below use) and fail loudly if its PR has already merged, instead of
    # silently dispatching a worker whose commits would land on a dead
    # branch with no open PR for review/merge to ever pick up (#947/#1115 —
    # a day-long invisible strand). This is defence-in-depth on top of
    # #1171's branch-per-slice fix: it also catches a *retry* of the SAME
    # (tracking_issue, issue_number) pair after that slice's own PR already
    # merged (e.g. via #1138's oracle gate), and a stale milestone-mode
    # dispatch after Gate A's shared-suite PR merged out from under it.
    from coord.agent import _slugify  # noqa: PLC0415

    branch = target_branch or f"issue-{tracking_issue}-{_slugify(assignment_title)}"

    if github_ops.pr_is_merged(repo_cfg.github, branch):
        raise RuntimeError(
            f"branch {branch!r} already has a merged PR — dispatching would "
            "push new commits onto a dead branch with nothing left to open "
            "a PR against them (#1172). "
            + (
                f"Issue #{issue_number}'s JIT slice already landed; if it "
                "needs more tests, this needs a fresh branch (not "
                "auto-forked yet) — do not retry as-is."
                if issue_number is not None else
                "The milestone's Gate-A suite PR already merged; if the "
                "suite needs more work, open a fresh branch by hand — do "
                "not retry as-is."
            )
        )

    deny_commands = test_author_deny_commands(config, repo_name)

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
    if target_branch:
        payload["target_branch"] = target_branch

    url = f"http://{machine.host}:{AGENT_PORT}/assign"
    client = http_client or httpx
    resp = client.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    agent_response = resp.json()

    assignment_id = agent_response.get("id") or uuid.uuid4().hex[:12]

    # #1171: record the deterministic branch name up front (mirrors #706's
    # `state._record_dispatched_local`) instead of leaving it NULL for
    # `reconcile.py`'s `issue-{tracking_issue}-*` backfill sweep (#1083) to
    # guess later — that sweep's prefix search can never find a JIT slice's
    # `target_branch` (deliberately outside the `issue-{N}-*` namespace, see
    # above), so the branch must be known at dispatch time here. (`branch`
    # itself was already resolved above, ahead of the POST, for the #1172
    # merged-PR guard.)

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
        branch=branch,
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


def dispatch_test_author_interactive(
    repo_name: str,
    tracking_issue: int,
    config: Config,
    *,
    issue_number: int | None = None,
    machine_override: str | None = None,
    path: str | None = None,
    dry_run: bool = False,
) -> int:
    """Human-attended counterpart to :func:`dispatch_test_author` (#1173,
    ``coord acceptance author --interactive``).

    Reuses the SAME interactive-launch machinery every other attended stage
    uses, instead of rebuilding any of it:

    * :func:`coord.commands.dispatch._build_interactive_launch_setup` for the
      shared ``ClaudePtyProvider`` / local-vs-remote detection / per-issue
      context digest (#603) that every ``coord assign --interactive`` flavour
      shares.
    * :func:`coord.agent.setup_interactive_worktree` (local) / a raw
      ``git worktree add`` shell command over ssh+tmux (remote) — the same
      primitives :func:`~coord.commands.dispatch_workers._dispatch_rework_of`
      uses to land on a NAMED branch rather than a fresh one.
    * :func:`coord.interactive.launch_human_attended_interactive` /
      :func:`~coord.interactive.finalize_interactive_exit` /
      :func:`~coord.interactive.finalize_remote_interactive_exit` for the
      actual PTY/tmux attach and the #466 git-floor completion backstop.

    The session lands on the EXACT same derived branch a headless dispatch
    of the same milestone/JIT slice would use — ``issue-{tracking_issue}-
    {slug(assignment_title)}``, the same derivation
    :meth:`coord.agent.AgentServer._setup_worktree` uses for
    ``type="test-author"`` — so headless and interactive test-author
    dispatches continue the SAME branch/PR rather than forking one each.

    Keeps the independence seal unchanged: the briefing content is built by
    the SAME :func:`build_test_author_briefing` the headless path uses, and
    the system prompt is :data:`TEST_AUTHOR_INTERACTIVE_SYSTEM_PROMPT` — the
    headless prompt plus a one-paragraph "an operator is watching" note.
    ``--interactive`` changes who supervises the authoring, never who writes
    the tests or what they may read.

    Records a ``type="test-author"`` :class:`~coord.models.Assignment` with
    ``provider_name="claude-pty"`` BEFORE launching (mirrors every other
    interactive flavour) so the board always reflects the in-flight session.
    That field is also what keeps this row out of automatic headless review
    dispatch (#555's generic ``provider_name != "claude-pty"`` guard in
    :func:`coord.review.dispatch_pending_reviews` — ``test-author`` is
    already in :data:`coord.models.WORK_LIKE_TYPES`, so no type-specific
    exclusion was needed). The explicit human-attended handoffs
    (``coord review``, ``coord assign --interactive --review-of/--merge-of``)
    pick this row up exactly like an interactive ``work`` completion — same
    board membership, same ``.branch`` — so there's no new stall to plumb
    around, only the same "human drives Test→Review→Merge" path #1173 asks
    for.

    Returns the child process's exit code (``0`` on a clean exit, or on a
    dry run). Raises :class:`RuntimeError` on any resolution failure — the
    same failure modes as :func:`dispatch_test_author` (unknown repo/driver/
    milestone/issue-membership/machine), plus a worktree/remote-launch setup
    failure (mirrors ``--rework-of``'s #618 failure-reason backstop: the
    reason is recorded on the assignment row before the error is raised, so
    the TUI can explain a red box with no log file).
    """
    from coord.agent import (  # noqa: PLC0415
        AssignmentSpec,
        _GitError,
        _slugify,
        setup_interactive_worktree,
    )
    from coord.interactive import (  # noqa: PLC0415
        TmuxHost,
        _launch_via_tmux,
        finalize_interactive_exit,
        finalize_remote_interactive_exit,
        launch_human_attended_interactive,
        tmux_available,
        tmux_session_alive,
        tmux_session_name,
    )
    from coord.state import (  # noqa: PLC0415
        build_board,
        record_dispatched_assignment,
        save_board,
        set_assignment_failure_reason,
    )

    repo_cfg = config.repo(repo_name)
    if repo_cfg is None:
        raise RuntimeError(f"repo {repo_name!r} not in coordinator.yml")

    driver_cfg = config.acceptance.driver_for(repo_name, path)
    if driver_cfg is None:
        if config.acceptance.has_driver(repo_name):
            raise RuntimeError(
                f"repo {repo_name!r} has a routed acceptance driver "
                "(acceptance.drivers routes) but no route matched — pass "
                "--for-path to select the milestone's subtree (e.g. "
                "'coord/**')"
            )
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

    repo_path_cfg = machine.repo_path(repo_name)
    if repo_path_cfg is None:
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

    # Same FIXED title as the headless dispatch (see dispatch_test_author's
    # comment above `assignment_title`) so both modes derive the SAME
    # branch name and continue the same branch/PR across repeated dispatches.
    assignment_title = f"[test-author] ms-{ctx.milestone_number} acceptance suite"
    repo_deny = repo_cfg.worker_permissions.deny if repo_cfg.worker_permissions else []
    deny_commands = list(dict.fromkeys(list(repo_deny) + TEST_AUTHOR_DENY_COMMANDS))
    default_branch = repo_cfg.default_branch or "main"
    branch_name = f"issue-{tracking_issue}-{_slugify(assignment_title)}"

    from coord.commands.dispatch import _build_interactive_launch_setup  # noqa: PLC0415

    setup = _build_interactive_launch_setup(
        machine=machine.name, repo=repo_name, issue=tracking_issue, machine_obj=machine,
    )
    provider = setup.provider
    is_local = setup.is_local
    issue_ctx = setup.issue_ctx
    svc = setup.svc

    if is_local:
        ta_repo_path = str(Path(repo_path_cfg).expanduser())
    else:
        ta_repo_path = repo_path_cfg

    resolved_model = config.models.default
    assignment_id = uuid.uuid4().hex[:12]

    scope = f"issue #{issue_number} slice" if issue_number is not None else "full milestone"
    report_reminder = (
        f"[Coordinator test-author assignment {assignment_id}] HUMAN-ATTENDED "
        f"interactive test-authoring ({scope}) for {repo_cfg.github} milestone "
        f"#{ctx.milestone_number} (tracking issue #{tracking_issue}). Before "
        f"you exit, run `coord report-result --assignment {assignment_id} "
        "--status <done|blocked> --summary <text>` so the coordinator "
        "records the result.\n\n"
    )
    effective_briefing = issue_ctx + report_reminder + briefing

    spec = AssignmentSpec(
        repo_name=repo_name,
        repo_path=ta_repo_path,
        issue_number=tracking_issue,
        issue_title=assignment_title,
        briefing=effective_briefing,
        model=resolved_model,
        type="test-author",
        provider="claude-pty",
        system_prompt=TEST_AUTHOR_INTERACTIVE_SYSTEM_PROMPT,
        deny_commands=deny_commands,
    )
    argv = provider.build_command(spec, resolved_model=resolved_model)
    # Remote: a bare "claude" is not on the SSH login PATH (#424/#425).
    if not is_local:
        argv = ["~/.local/bin/claude"] + list(argv)[1:]

    location = "local TTY" if is_local else f"{machine.host} (remote tmux)"
    click.echo(
        f"{machine.name} ({location}) → TEST-AUTHOR for {repo_cfg.github} "
        f"milestone #{ctx.milestone_number} (tracking issue #{tracking_issue}, "
        f"{scope})"
    )
    click.echo("  mode: HUMAN-ATTENDED interactive test-authoring (#1173)")
    click.echo(f"  assignment id: {assignment_id}  (branch: {branch_name})")

    if dry_run:
        click.echo("  (dry run — not launched)")
        click.echo(f"  would exec: {argv}")
        return 0

    ta_assignment = Assignment(
        machine_name=machine.name,
        repo_name=repo_name,
        issue_number=tracking_issue,
        issue_title=assignment_title,
        briefing=effective_briefing,
        assignment_id=assignment_id,
        status="running",
        branch=branch_name,
        dispatched_at=time.time(),
        type="test-author",
        for_issue_number=issue_number,
        model=resolved_model,
        provider_name="claude-pty",
    )
    record_dispatched_assignment(assignment=ta_assignment, repo_github=repo_cfg.github)
    if svc is None:
        save_board(build_board())
    os.environ["COORD_ASSIGNMENT_ID"] = assignment_id

    if is_local:
        try:
            wt_path, _ = setup_interactive_worktree(
                Path(ta_repo_path),
                issue_number=tracking_issue,
                issue_title=assignment_title,
                assignment_id=assignment_id,
                default_branch=default_branch,
                existing_branch=branch_name,
            )
            worktree_path = str(wt_path)
        except (_GitError, OSError) as wt_err:
            reason = f"worktree-add failed for branch {branch_name}: {wt_err}"
            click.echo(f"  error: {reason}", err=True)
            set_assignment_failure_reason(assignment_id, reason)
            raise RuntimeError(reason) from wt_err
        click.echo(f"  worktree: {worktree_path} (branch: {branch_name})")

        started_at = time.time()
        exit_code = launch_human_attended_interactive(
            argv, effective_briefing, assignment_id=assignment_id, cwd=worktree_path,
        )
        if exit_code != 0:
            click.echo(f"  claude exited with status {exit_code}", err=True)

        sname = tmux_session_name(assignment_id) if tmux_available() else None
        if sname and tmux_session_alive(sname):
            click.echo(
                f"  session still running in tmux: {sname}\n"
                f"  reattach with:  coord reattach {assignment_id}"
            )
            return 0

        try:
            finalize_result = finalize_interactive_exit(
                assignment_id=assignment_id,
                repo_name=repo_name,
                repo_github=repo_cfg.github,
                issue_number=tracking_issue,
                machine_name=machine.name,
                worktree_path=worktree_path,
                base_branch=default_branch,
                exit_code=exit_code,
                started_at=started_at,
                log_path=None,
                repo_path=ta_repo_path,
                branch=branch_name,
            )
            if finalize_result.already_recorded:
                click.echo(
                    "  result recorded via `coord report-result`; backstop "
                    "did not overwrite"
                )
            else:
                click.echo(
                    f"  backstop: status={finalize_result.terminal_status} "
                    f"commits_ahead={finalize_result.commits_ahead}"
                )
                if not finalize_result.push_ok:
                    click.echo(
                        f"  warning: git push failed: {finalize_result.push_error}",
                        err=True,
                    )
        except Exception as exc:  # noqa: BLE001 — best-effort backstop
            click.echo(
                f"  warning: backstop failed to record test-author exit: {exc}",
                err=True,
            )
        return exit_code

    # ── REMOTE (#1173) ──────────────────────────────────────────────────
    # Mirrors _dispatch_rework_of's remote shape (named-branch continuation
    # + finalize_remote_interactive_exit) but WITHOUT its holder-detection
    # retry maze (#759/#814) — test-author dispatch is a low-frequency Gate-A
    # / JIT call, not the hot auto-loop path, so a branch/worktree collision
    # is far less likely than for --rework-of's "resume a specific in-flight
    # session" case. Lift that block from dispatch_workers._dispatch_rework_of
    # if this turns out to need it.
    import shlex

    remote_wt = "$HOME/.coord/worktrees/" + assignment_id
    rp_sh = (
        "$HOME/" + ta_repo_path[2:]
        if ta_repo_path.startswith("~/")
        else ("$HOME" if ta_repo_path == "~" else ta_repo_path)
    )
    claude_args = shlex.join(list(argv)[1:])
    br_q = shlex.quote(branch_name)
    orig_ref = shlex.quote(f"origin/{branch_name}")
    remote_cmd = (
        f"mkdir -p $HOME/.coord/worktrees"
        f" && cd {rp_sh}"
        f" && git fetch origin --prune 2>/dev/null || true"
        f" && git worktree prune 2>/dev/null || true"
        f" && (git worktree add -B {br_q} {remote_wt} {orig_ref} 2>/dev/null"
        f" || git worktree add -b {br_q} {remote_wt} origin/{default_branch})"
        f" && cd {remote_wt}"
        f" && COORD_ASSIGNMENT_ID={assignment_id} {argv[0]} {claude_args}"
    )
    tmux_host = TmuxHost(ssh_target=machine.host)
    sname = tmux_session_name(assignment_id)
    click.echo(
        f"  remote worktree: $HOME/.coord/worktrees/{assignment_id} on "
        f"{machine.host} (branch: {branch_name})"
    )

    if effective_briefing.strip():
        hdr = (
            "--- seeded briefing -- review below; "
            "submit the pre-filled input in Claude to send ---"
        )
        ftr = "-" * len(hdr)
        preview = f"\n{hdr}\n{effective_briefing.rstrip()}\n{ftr}\n\n"
        try:
            os.write(sys.stdout.fileno(), preview.encode("utf-8"))
        except OSError:
            pass

    started_at = time.time()
    rc = _launch_via_tmux(
        argv, effective_briefing, sname, cwd=None, host=tmux_host,
        raw_shell_cmd=remote_cmd,
    )
    if rc is None:
        reason = f"could not create remote tmux session on {machine.host}"
        click.echo(f"  error: {reason}", err=True)
        set_assignment_failure_reason(assignment_id, reason)
        raise RuntimeError(reason)
    exit_code = rc

    if tmux_session_alive(sname, host=tmux_host):
        click.echo(
            f"  session still running in remote tmux: {sname}\n"
            f"  reattach with:  ssh -t {machine.host} tmux attach-session -t {sname}"
        )
        return 0

    try:
        remote_result = finalize_remote_interactive_exit(
            assignment_id=assignment_id,
            repo_name=repo_name,
            repo_github=repo_cfg.github,
            issue_number=tracking_issue,
            machine_name=machine.name,
            ssh_target=machine.host,
            remote_worktree_sh=remote_wt,
            remote_repo_sh=rp_sh,
            branch=branch_name,
            base_branch=default_branch,
            exit_code=exit_code,
            started_at=started_at,
        )
        if remote_result.already_recorded:
            click.echo(
                "  result recorded via `coord report-result`; remote "
                "backstop did not overwrite"
            )
        else:
            click.echo(
                f"  remote backstop: status={remote_result.terminal_status} "
                f"commits_ahead={remote_result.commits_ahead} "
                f"pushed={remote_result.push_ok}"
            )
            if not remote_result.push_ok:
                click.echo(
                    f"  warning: remote push failed: {remote_result.push_error}",
                    err=True,
                )
    except Exception as exc:  # noqa: BLE001 — best-effort backstop
        click.echo(
            f"  warning: remote backstop failed to record test-author exit: {exc}",
            err=True,
        )
    return exit_code
