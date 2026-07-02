"""`coord milestone` — Phase 0 of #767 (milestone-driven workflow).

Thin CLI glue around ``coord/milestone_order.py``: fetches the tracking
issue + milestone membership + issue terminal state from GitHub, then hands
that data to the pure parser/DAG/frontier functions and prints the result.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from coord.commands._common import _CONFIG_OPTION, _load_config
from coord.milestone_order import (
    WorkOrderError,
    parse_work_order,
    ready_frontier,
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

    from coord import board_service, github_ops

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

    body = issue_data.get("body") or ""
    try:
        work_order = parse_work_order(body)
    except WorkOrderError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    if not work_order.nodes:
        click.echo(f"#{tracking_issue}: no `## Work order` block found")
        return

    # Membership + terminal state. Issues currently open under the milestone
    # come free from one `get_open_issues` call; anything a node references
    # that isn't in that set gets an individual lookup (closed, or foreign).
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
        try:
            node_data = github_ops.get_issue(repo_entry.github, node.issue_number)
        except RuntimeError as e:
            click.echo(
                f"error: could not fetch #{node.issue_number}: {e}", err=True
            )
            sys.exit(1)
        node_milestone_number = (node_data.get("milestone") or {}).get("number")
        if node_milestone_number == milestone_number:
            milestone_issue_numbers.add(node.issue_number)
        if node_data.get("state", "").upper() == "CLOSED":
            terminal_issues.add(node.issue_number)

    try:
        validate_milestone_membership(work_order, milestone_issue_numbers)
    except WorkOrderError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    board = board_service.read_board()
    frontier = ready_frontier(
        work_order,
        board,
        repo_name=repo_entry.name,
        repo_github=repo_entry.github,
        terminal_issues=terminal_issues,
    )

    click.echo(
        f"Work order for #{tracking_issue} (milestone #{milestone_number}):"
    )
    for node in work_order.nodes:
        state = "done" if node.issue_number in terminal_issues else "open"
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
