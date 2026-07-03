"""`coord milestone` — Phase 0 (`order`) + Phase 1 (`dispatch`) of #767
(milestone-driven workflow).

Thin CLI glue around ``coord/milestone_order.py`` (the pure DAG/frontier
parser) and ``coord/milestone_dispatch.py`` (Phase 1's machine-picking +
dispatch): fetches the tracking issue + milestone membership + issue
terminal state from GitHub via ``coord.milestone_dispatch.
fetch_milestone_context`` (shared by both subcommands so they see identical
inputs), then hands that data to the pure functions and prints the result.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from coord.commands._common import _CONFIG_OPTION, _load_config
from coord.milestone_dispatch import (
    DispatchOutcome,
    MachinePick,
    MilestoneDispatchError,
    dispatch_entry,
    fetch_milestone_context,
    is_milestone_complete,
    plan_dispatch,
)
from coord.milestone_order import (
    WorkOrder,
    WorkOrderError,
    parse_work_order,
    replace_work_order_section,
    validate_milestone_membership,
)


@click.group("milestone")
def milestone_group() -> None:
    """Milestone work-order operations (#767 Phase 0) + the milestone write
    seam (#645)."""


@milestone_group.command(
    "create",
    help=(
        "Create a GitHub milestone through the backend-agnostic tracker seam. "
        "REPO is the local repo name from coordinator.yml. Prints the new "
        "milestone number on success. Routes through the daemon seam so "
        "agents (notably the milestone-chat session) never call `gh` "
        "directly."
    ),
)
@click.argument("repo")
@click.option("--title", required=True, help="Milestone title.")
@click.option("--description", default=None, help="Milestone description (markdown).")
@click.option(
    "--due-on",
    default=None,
    help="Due date, ISO 8601 (e.g. 2026-08-01T00:00:00Z).",
)
@_CONFIG_OPTION
def milestone_create_cmd(
    repo: str,
    title: str,
    description: str | None,
    due_on: str | None,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    from coord.state import write_milestone

    try:
        result = write_milestone(
            repo,
            title=title,
            description=description,
            due_on=due_on,
            repo_github=repo_entry.github,
        )
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: milestone create failed: {e}", err=True)
        sys.exit(1)
    click.echo(
        f"milestone #{result.get('number')} created ({repo_entry.github}): "
        f"{result.get('title')}"
    )


@milestone_group.command(
    "edit",
    help=(
        "Edit a GitHub milestone's title/description/due date. REPO is the "
        "local repo name from coordinator.yml; NUMBER is the GH milestone "
        "number. Provide at least one of --title/--description/--due-on. "
        "Routes through the backend-agnostic tracker seam."
    ),
)
@click.argument("repo")
@click.argument("number", type=int)
@click.option("--title", default=None, help="New milestone title.")
@click.option("--description", default=None, help="New milestone description (markdown).")
@click.option(
    "--due-on",
    default=None,
    help="New due date, ISO 8601 (e.g. 2026-08-01T00:00:00Z).",
)
@_CONFIG_OPTION
def milestone_edit_cmd(
    repo: str,
    number: int,
    title: str | None,
    description: str | None,
    due_on: str | None,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)
    if title is None and description is None and due_on is None:
        click.echo(
            "error: provide --title and/or --description and/or --due-on",
            err=True,
        )
        sys.exit(2)

    from coord.state import write_milestone

    try:
        write_milestone(
            repo,
            number=number,
            title=title,
            description=description,
            due_on=due_on,
            repo_github=repo_entry.github,
        )
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: milestone edit failed: {e}", err=True)
        sys.exit(1)
    click.echo(f"milestone #{number} updated ({repo_entry.github})")


@milestone_group.command(
    "order",
    help=(
        "Parse the `## Work order` block from a milestone tracking issue and "
        "print the DAG + current ready frontier. REPO is the local repo name "
        "from coordinator.yml; TRACKING_ISSUE is the GH issue number of the "
        "tracking issue (its body holds the `## Work order` block)."
    ),
)
@click.argument("repo")
@click.argument("tracking_issue", type=int)
@_CONFIG_OPTION
def milestone_order_cmd(repo: str, tracking_issue: int, config_path: Path) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    from coord import board_service

    try:
        ctx = fetch_milestone_context(repo_entry, tracking_issue)
    except MilestoneDispatchError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    if not ctx.work_order.nodes:
        click.echo(f"#{tracking_issue}: no `## Work order` block found")
        return

    board = board_service.read_board()
    from coord.milestone_order import ready_frontier

    frontier = ready_frontier(
        ctx.work_order,
        board,
        repo_name=repo_entry.name,
        repo_github=repo_entry.github,
        terminal_issues=set(ctx.terminal_issues),
    )

    click.echo(
        f"Work order for #{tracking_issue} (milestone #{ctx.milestone_number}):"
    )
    for node in ctx.work_order.nodes:
        state = "done" if node.issue_number in ctx.terminal_issues else "open"
        bits = [f"[{state}]"]
        if node.group:
            bits.append(f"group:{node.group}")
        if node.after:
            bits.append("after:" + ",".join(f"#{d}" for d in node.after))
        click.echo(f"  #{node.issue_number}  {' '.join(bits)}")

    click.echo()
    click.echo("Ready frontier:")
    if frontier.ready:
        for entry in frontier.ready:
            suffix = f"  (group {entry.group})" if entry.group else ""
            click.echo(f"  #{entry.issue_number}{suffix}")
    else:
        click.echo("  (none)")

    if frontier.blocked:
        click.echo()
        click.echo("Blocked:")
        for b in frontier.blocked:
            click.echo(f"  #{b.issue_number}: {b.reason}")


def _echo_pick(pick: MachinePick) -> None:
    suffix = f"  (group {pick.entry.group})" if pick.entry.group else ""
    click.echo(f"  #{pick.entry.issue_number} -> {pick.machine.name}{suffix}")


def _echo_outcome(outcome: DispatchOutcome) -> None:
    if outcome.ok:
        click.echo(
            f"  #{outcome.issue_number} -> {outcome.machine_name} "
            f"(dispatched, assignment {outcome.assignment_id})"
        )
    else:
        click.echo(
            f"  #{outcome.issue_number}: dispatch failed: {outcome.error}", err=True
        )


@milestone_group.command(
    "dispatch",
    help=(
        "Promote a milestone into the pipeline: dispatch its ready frontier "
        "in parallel (up to idle/capable machines), then keep draining it as "
        "declared-order dependencies complete. REPO is the local repo name "
        "from coordinator.yml; TRACKING_ISSUE is the GH issue number of the "
        "tracking issue (its body holds the `## Work order` block). This is "
        "the single explicit approval for the whole declared work order — "
        "it does not expand scope beyond what `## Work order` lists."
    ),
)
@click.argument("repo")
@click.argument("tracking_issue", type=int)
@click.option(
    "--dry-run", is_flag=True,
    help="Show what would dispatch now vs. what's waiting (and on what), without dispatching.",
)
@click.option(
    "--next", "next_", is_flag=True,
    help=(
        "Single-pick mode: show up to 3 ready-frontier items and dispatch "
        "only the one you choose. Lighter-weight than draining the whole "
        "milestone — does not register for daemon auto-drain."
    ),
)
@_CONFIG_OPTION
def milestone_dispatch_cmd(
    repo: str, tracking_issue: int, dry_run: bool, next_: bool, config_path: Path
) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    from coord import board_service

    try:
        ctx = fetch_milestone_context(repo_entry, tracking_issue)
    except MilestoneDispatchError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    if not ctx.work_order.nodes:
        click.echo(f"#{tracking_issue}: no `## Work order` block found")
        return

    board = board_service.read_board()
    plan = plan_dispatch(ctx.work_order, board, cfg, repo_entry, ctx.terminal_issues)

    click.echo(
        f"Work order for #{tracking_issue} (milestone #{ctx.milestone_number}):"
    )

    if next_:
        if not plan.to_dispatch:
            click.echo("No ready frontier item can dispatch right now.")
            if plan.skipped:
                click.echo("Ready but no idle machine:")
                for s in plan.skipped:
                    click.echo(f"  #{s.entry.issue_number}: {s.reason}")
            if plan.waiting:
                click.echo("Waiting:")
                for b in plan.waiting:
                    click.echo(f"  #{b.issue_number}: {b.reason}")
            return

        choices = plan.to_dispatch[:3]
        click.echo("Ready frontier — pick one to dispatch:")
        for i, pick in enumerate(choices, 1):
            suffix = f"  (group {pick.entry.group})" if pick.entry.group else ""
            click.echo(f"  {i}. #{pick.entry.issue_number} -> {pick.machine.name}{suffix}")

        if dry_run:
            click.echo("(dry run — not dispatched)")
            return

        idx = click.prompt(
            "Pick one", type=click.IntRange(1, len(choices)), default=1
        )
        chosen = choices[idx - 1]
        outcome = dispatch_entry(chosen, repo_entry, cfg, board, tracking_issue=tracking_issue)
        _echo_outcome(outcome)
        if not outcome.ok:
            sys.exit(1)
        return

    # Bulk mode: dispatch the entire current ready frontier.
    click.echo("Will dispatch now:")
    if plan.to_dispatch:
        for pick in plan.to_dispatch:
            _echo_pick(pick)
    else:
        click.echo("  (none)")

    if plan.skipped:
        click.echo()
        click.echo("Ready but no idle machine:")
        for s in plan.skipped:
            click.echo(f"  #{s.entry.issue_number}: {s.reason}")

    if plan.waiting:
        click.echo()
        click.echo("Waiting:")
        for b in plan.waiting:
            click.echo(f"  #{b.issue_number}: {b.reason}")

    if dry_run:
        click.echo()
        click.echo("(dry run — not dispatched)")
        return

    if not plan.to_dispatch:
        click.echo()
        click.echo("Nothing to dispatch right now.")
        return

    click.echo()
    failures = 0
    for pick in plan.to_dispatch:
        outcome = dispatch_entry(pick, repo_entry, cfg, board, tracking_issue=tracking_issue)
        _echo_outcome(outcome)
        if not outcome.ok:
            failures += 1

    if not is_milestone_complete(ctx):
        from coord.state import register_milestone_drain

        register_milestone_drain(repo_name=repo_entry.name, tracking_issue=tracking_issue)
        click.echo()
        click.echo(
            f"Milestone #{tracking_issue} registered for daemon auto-drain "
            "(requires `milestone.auto_dispatch: true` in coordinator.yml + a "
            "daemon restart to activate; `coord milestone dispatch` still "
            "works as a manual re-drain either way)."
        )

    if failures:
        sys.exit(1)


def _resolve_milestone_membership(
    repo_entry, milestone_number: int, work_order: WorkOrder
) -> tuple[set[int], set[int]]:
    """Resolve which of *work_order*'s issue numbers belong to the milestone
    and which have already reached a closed/terminal state.

    Open issues under the milestone come free from one ``get_open_issues``
    call; any work-order node not in that set gets an individual
    ``get_issue`` lookup (closed, or foreign) to classify it. Used by
    ``coord milestone write-order`` to validate proposed work-order content
    before writing it (``coord milestone order``/``dispatch`` resolve
    membership via ``milestone_dispatch.fetch_milestone_context`` instead).
    """
    from coord import github_ops

    open_issues = github_ops.get_open_issues(repo_entry.github)
    milestone_issue_numbers = {
        i["number"]
        for i in open_issues
        if (i.get("milestone") or {}).get("number") == milestone_number
    }
    terminal_issues: set[int] = set()
    for node in work_order.nodes:
        if node.issue_number in milestone_issue_numbers:
            continue
        node_data = github_ops.get_issue(repo_entry.github, node.issue_number)
        node_milestone_number = (node_data.get("milestone") or {}).get("number")
        if node_milestone_number == milestone_number:
            milestone_issue_numbers.add(node.issue_number)
        if node_data.get("state", "").upper() == "CLOSED":
            terminal_issues.add(node.issue_number)
    return milestone_issue_numbers, terminal_issues


@milestone_group.command(
    "write-order",
    help=(
        "#770 (Phase 2 of #767): validate and write a `## Work order` block "
        "into a milestone tracking issue.\n\n"
        "Reads the new checklist lines (e.g. `- [ ] #762  {group: A}`, no "
        "heading) from --file or stdin, splices them into the tracking "
        "issue's current body (replacing any existing `## Work order` "
        "section — idempotent, never duplicated), re-parses and validates "
        "the result (cycles, unknown `after` targets, milestone membership) "
        "BEFORE writing, then calls `github_ops.update_issue_body` — the "
        "`coord`, never-raw-`gh` write path #645/#770 require.\n\n"
        "REPO is the local repo name from coordinator.yml; TRACKING_ISSUE is "
        "the GH issue number of the tracking issue (must carry a milestone)."
    ),
)
@click.argument("repo")
@click.argument("tracking_issue", type=int)
@click.option(
    "--file",
    "block_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to a file with the new checklist lines. Reads stdin when omitted.",
)
@_CONFIG_OPTION
def milestone_write_order_cmd(
    repo: str, tracking_issue: int, block_file: Path | None, config_path: Path
) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    new_block = (
        block_file.read_text() if block_file is not None else sys.stdin.read()
    )
    if not new_block.strip():
        click.echo("error: no work-order content given (empty --file/stdin)", err=True)
        sys.exit(2)

    from coord import github_ops

    try:
        issue_data = github_ops.get_issue(repo_entry.github, tracking_issue)
    except RuntimeError as e:
        click.echo(f"error: could not fetch #{tracking_issue}: {e}", err=True)
        sys.exit(1)

    milestone = issue_data.get("milestone") or {}
    milestone_number = milestone.get("number")
    if milestone_number is None:
        click.echo(f"error: #{tracking_issue} has no milestone", err=True)
        sys.exit(1)

    old_body = issue_data.get("body") or ""
    candidate_body = replace_work_order_section(old_body, new_block)

    try:
        work_order = parse_work_order(candidate_body)
    except WorkOrderError as e:
        click.echo(f"error: proposed work order is invalid: {e}", err=True)
        sys.exit(1)

    if not work_order.nodes:
        click.echo(
            "error: proposed work order is empty — refusing to write an "
            "empty `## Work order` block",
            err=True,
        )
        sys.exit(1)

    try:
        milestone_issue_numbers, _terminal = _resolve_milestone_membership(
            repo_entry, milestone_number, work_order
        )
    except RuntimeError as e:
        click.echo(f"error: could not resolve milestone membership: {e}", err=True)
        sys.exit(1)

    try:
        validate_milestone_membership(work_order, milestone_issue_numbers)
    except WorkOrderError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    if candidate_body == old_body:
        click.echo(f"#{tracking_issue}: work order unchanged (idempotent no-op)")
        return

    github_ops.update_issue_body(repo_entry.github, tracking_issue, candidate_body)
    click.echo(
        f"#{tracking_issue}: wrote `## Work order` block "
        f"({len(work_order.nodes)} node(s))"
    )


@milestone_group.command(
    "chat",
    help=(
        "#770 (Phase 2 of #767): dispatch a milestone-steward chat session.\n\n"
        "Seeds a `type=\"milestone-chat\"` `claude -p` worker with the "
        "milestone tracking issue's current body and the open issues filed "
        "under the milestone, then prints the new assignment id to stdout. "
        "The steward discusses the milestone, proposes a `## Work order` "
        "block inferring parallel cohorts (`group`) vs. hard dependencies "
        "(`after`) from the issue bodies, and — only once the operator "
        "confirms in the conversation — writes it via `coord milestone "
        "write-order` (never raw `gh`).\n\n"
        "REPO is the local repo name from coordinator.yml; TRACKING_ISSUE is "
        "the GH issue number of the tracking issue (must carry a milestone)."
    ),
)
@click.argument("repo")
@click.argument("tracking_issue", type=int)
@click.option(
    "--machine",
    default=None,
    help="Override machine selection (default: first unpaused machine that lists the repo).",
)
@_CONFIG_OPTION
def milestone_chat_cmd(
    repo: str, tracking_issue: int, machine: str | None, config_path: Path
) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    from coord.milestone_chat import dispatch_milestone_chat

    try:
        assignment_id, _picked_machine = dispatch_milestone_chat(
            repo,
            tracking_issue,
            cfg,
            machine_override=machine,
        )
    except RuntimeError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    # Print the assignment id as the LAST stdout line so callers (the TUI,
    # eventually — #771/#645) can capture it with a "last non-empty line"
    # parse, matching refine-chat / new-issue-chat.
    click.echo(assignment_id)
