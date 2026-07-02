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
    WorkOrder,
    WorkOrderError,
    parse_work_order,
    ready_frontier,
    replace_work_order_section,
    validate_milestone_membership,
)


@click.group("milestone")
def milestone_group() -> None:
    """Milestone work-order operations (#767 Phase 0 + Phase 2)."""


def _resolve_milestone_membership(
    repo_entry, milestone_number: int, work_order: WorkOrder
) -> tuple[set[int], set[int]]:
    """Resolve which of *work_order*'s issue numbers belong to the milestone
    and which have already reached a closed/terminal state.

    Open issues under the milestone come free from one ``get_open_issues``
    call; any work-order node not in that set gets an individual
    ``get_issue`` lookup (closed, or foreign) to classify it. Shared by
    ``coord milestone order`` and ``coord milestone write-order`` so both
    commands agree on membership/terminal-state resolution.
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
    try:
        milestone_issue_numbers, terminal_issues = _resolve_milestone_membership(
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
