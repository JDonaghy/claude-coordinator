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
