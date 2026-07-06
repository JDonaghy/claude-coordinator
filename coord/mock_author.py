"""#930 (docs/ORACLE_LOOP.md, Gate A): dispatch a `type="mock-author"` agent.

Gate A is the milestone's pre-work architecture gate: before any of a
milestone's issues may dispatch, an independent agent — zero shared context
with the eventual workers, mirroring the adversarial code reviewer — renders
a viewable mock of the milestone's user-facing surface and pins the exact
black-box contract (CLI names, key screen text, API field shapes) that both
the workers and the independent test-author (#931) implement/test to.

Used by `coord acceptance mock <repo> <tracking_issue>` (the CLI command in
``coord/commands/acceptance.py``). Deliberately keyed by the milestone's
tracking-issue number, not a `--milestone NN` flag — every sibling milestone
command (`coord milestone order/dispatch/chat/gate-c`) takes
`<repo> <tracking_issue>`, and the milestone number itself is resolved from
the tracking issue, same as `coord/milestone_chat.py` already does. The
on-disk artifact directory is still `tests/acceptance/ms-<milestone_number>/`
per ORACLE_LOOP.md's settled layout.

The mock-author gets a real git worktree + branch (unlike the read-only
`milestone-chat` type) since its whole job is committing files under
`tests/acceptance/ms-NN/` — see `type="mock-author"` handling in
``coord/agent.py`` (``MOCK_AUTHOR_SYSTEM_PROMPT`` / ``WRITE_CAPABLE_SPEC_
TYPES``) and the matching exemption from the acceptance-dir auto-forbid in
``coord/dispatch.py``. It dispatches through the same
Work → Test → Review → Merge pipeline as any other branch (`required_gates`
is the repo's normal `default_gates`) — Gate A produces a normal reviewed
commit, not a special-cased one.
"""
from __future__ import annotations

import uuid

from coord import github_ops
from coord.acceptance import ms_dirname
from coord.claim import claim_message, find_work_claim
from coord.config import Config
from coord.milestone_chat import _fetch_milestone_issues
from coord.milestone_dispatch import pick_machine
from coord.models import Machine, Proposal


def build_mock_author_briefing(
    *,
    repo_slug: str,
    milestone_title: str,
    milestone_number: int,
    tracking_issue_number: int,
    tracking_issue_body: str,
    issues: list[dict],
    driver_kind: str,
    driver_mock_glob: str,
) -> str:
    """Compose the seed briefing the mock-author sees as its first user
    message: milestone context + exactly where its output must land."""
    ms_dir = f"tests/acceptance/{ms_dirname(milestone_number)}"

    parts: list[str] = []
    parts.append(
        f"=== Gate A mock-author context for {repo_slug} "
        f"— milestone {milestone_title!r} ===\n"
    )

    parts.append(f"TRACKING ISSUE: #{tracking_issue_number}")
    parts.append("MILESTONE TRACKING ISSUE BODY:")
    parts.append(tracking_issue_body.strip() if tracking_issue_body.strip() else "(empty)")
    parts.append("")

    parts.append(f"OPEN ISSUES UNDER THIS MILESTONE ({len(issues)}):")
    if issues:
        for issue in issues:
            parts.append(f"--- #{issue['number']}: {issue['title']} ---")
            parts.append(issue["body"] if issue["body"] else "(no body)")
            parts.append("")
    else:
        parts.append("  (none fetched)")

    parts.append("---")
    parts.append(
        f"This repo's acceptance driver is `{driver_kind}` — render the "
        f"mock(s) in that medium as `{driver_mock_glob}` fixtures under "
        f"`{ms_dir}/mocks/`, then write the black-box contract to "
        f"`{ms_dir}/contract.md` (docs/ORACLE_LOOP.md \"Layout\"). If "
        f"`{ms_dir}/contract.md` already exists, you are AMENDING Gate A, "
        "not authoring it from scratch — read it first and edit in place. "
        "Commit and push both to this branch when done."
    )
    return "\n".join(parts)


def dispatch_acceptance_mock(
    repo_name: str,
    tracking_issue_number: int,
    config: Config,
    *,
    machine_override: str | None = None,
) -> tuple[str, str]:
    """End-to-end: resolve the milestone, pick a machine, seed the
    briefing, dispatch a ``type="mock-author"`` assignment.

    Returns ``(assignment_id, machine_name)``. Raises ``RuntimeError`` when
    the repo is unknown, has no acceptance driver configured, the tracking
    issue can't be fetched or has no milestone, the milestone's Gate A is
    already claimed, no machine claims the repo, or the agent rejects the
    dispatch.
    """
    repo_cfg = config.repo(repo_name)
    if repo_cfg is None:
        raise RuntimeError(f"repo {repo_name!r} not in coordinator.yml")

    driver_cfg = config.acceptance.driver_for(repo_name)
    if driver_cfg is None:
        raise RuntimeError(
            f"repo {repo_name!r} has no acceptance driver configured — add "
            "it under acceptance.drivers in coordinator.yml before running "
            "Gate A (docs/ORACLE_LOOP.md)"
        )

    try:
        issue_data = github_ops.get_issue(repo_cfg.github, tracking_issue_number)
    except RuntimeError as e:
        raise RuntimeError(f"could not fetch #{tracking_issue_number}: {e}") from e

    milestone = issue_data.get("milestone") or {}
    milestone_number = milestone.get("number")
    if milestone_number is None:
        raise RuntimeError(f"#{tracking_issue_number} has no milestone")
    milestone_title = milestone.get("title") or f"#{milestone_number}"

    from coord import board_service  # noqa: PLC0415

    board = board_service.read_board()

    claim = find_work_claim(tracking_issue_number, repo_name, repo_cfg.github, board)
    if claim is not None:
        raise RuntimeError(f"Gate A already in flight: {claim_message(claim)}")

    # Pick the machine.
    if machine_override:
        machine = next(
            (m for m in config.machines if m.name == machine_override),
            None,
        )
        if machine is None:
            raise RuntimeError(f"machine {machine_override!r} not in coordinator.yml")
        if not machine.can_work_on(repo_name):
            raise RuntimeError(
                f"machine {machine_override!r} does not list repo {repo_name!r}"
            )
    else:
        picked: Machine | None = pick_machine(repo_name, board, config)
        if picked is None:
            raise RuntimeError(
                f"no idle machine claims repo {repo_name!r} — mock-author "
                "needs a machine that has the repo cloned"
            )
        machine = picked

    issues = _fetch_milestone_issues(repo_cfg.github, milestone_number)

    briefing = build_mock_author_briefing(
        repo_slug=repo_cfg.github,
        milestone_title=milestone_title,
        milestone_number=milestone_number,
        tracking_issue_number=tracking_issue_number,
        tracking_issue_body=issue_data.get("body") or "",
        issues=issues,
        driver_kind=driver_cfg.kind,
        driver_mock_glob=driver_cfg.mock,
    )

    tracking_title = issue_data.get("title") or f"Milestone #{tracking_issue_number}"
    resolved_model = config.models.default
    proposal = Proposal(
        id=0,
        machine_name=machine.name,
        repo_name=repo_name,
        issue_number=tracking_issue_number,
        issue_title=f"[gate-a] {tracking_title} — mock + contract",
        rationale="Gate A mock-author dispatch (coord acceptance mock, #930)",
        briefing=briefing,
        model=resolved_model,
        type="mock-author",
        required_gates=list(config.pipeline.default_gates),
        target_branch=f"ms-{milestone_number}-gate-a",
    )

    from coord.dispatch import dispatch_with_retry, post_briefing  # noqa: PLC0415
    from coord.state import record_dispatched  # noqa: PLC0415

    response = dispatch_with_retry(
        proposal,
        config,
        max_retries=config.concurrency.max_retries,
        backoff_base=config.concurrency.backoff_base,
    )

    assignment_id = response.get("id") or uuid.uuid4().hex[:12]

    record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github=repo_cfg.github,
        provider_name=response.get("_provider_name"),
    )

    try:
        post_briefing(proposal, config, assignment_id=assignment_id)
    except Exception:  # noqa: BLE001 — best-effort, mirrors dispatch_entry
        pass

    return assignment_id, machine.name
