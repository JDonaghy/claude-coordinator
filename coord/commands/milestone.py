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

import json
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
    gate_a_status,
    is_milestone_complete,
    plan_dispatch,
)
from coord.milestone_order import (
    WorkOrder,
    WorkOrderError,
    WorkOrderNode,
    parse_sub_issues,
    parse_work_order,
    render_sub_issues,
    replace_sub_issues_section,
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
    "assign",
    help=(
        "Assign an existing issue to a milestone. REPO is the local repo name "
        "from coordinator.yml; ISSUE is the GH issue number; MILESTONE is the "
        "milestone number (e.g. `5`) or title (e.g. `v1.0`). Titles are "
        "resolved to their number by fetching open milestones from GitHub.\n\n"
        "Routes through the daemon seam (``POST /issue-milestone``) so the "
        "write is always traceable and the local issues cache "
        "``milestone_number``/``milestone_title`` columns are updated "
        "immediately (no need to wait for ``coord sync``)."
    ),
)
@click.argument("repo")
@click.argument("issue", type=int)
@click.argument("milestone")
@_CONFIG_OPTION
def milestone_assign_cmd(
    repo: str,
    issue: int,
    milestone: str,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    from coord import github_ops  # noqa: PLC0415
    from coord.state import assign_issue_milestone  # noqa: PLC0415

    # Resolve milestone argument: numeric → number, string → title lookup.
    milestone_number: int
    milestone_title: str | None

    try:
        milestone_number = int(milestone)
        # Resolve the title from the number so the cache can store it.
        try:
            ms_data = github_ops.get_milestone(repo_entry.github, milestone_number)
            milestone_title = ms_data.get("title")
        except RuntimeError as e:
            click.echo(
                f"error: could not fetch milestone #{milestone_number}: {e}", err=True
            )
            sys.exit(1)
    except ValueError:
        # Treat the argument as a title and resolve to a number.
        try:
            all_ms = github_ops.get_repo_milestones(repo_entry.github)
        except RuntimeError as e:
            click.echo(f"error: could not list milestones: {e}", err=True)
            sys.exit(1)
        matches = [m for m in all_ms if m.get("title") == milestone]
        if not matches:
            click.echo(
                f"error: no open milestone with title {milestone!r} in {repo_entry.github}",
                err=True,
            )
            sys.exit(1)
        if len(matches) > 1:
            click.echo(
                f"error: multiple open milestones match {milestone!r} — "
                "use the milestone number instead",
                err=True,
            )
            sys.exit(1)
        milestone_number = matches[0]["number"]
        milestone_title = matches[0].get("title")

    try:
        assign_issue_milestone(
            repo,
            issue,
            milestone_number,
            milestone_title=milestone_title,
            repo_github=repo_entry.github,
        )
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: milestone assign failed: {e}", err=True)
        sys.exit(1)

    ms_label = (
        f"{milestone_title!r} (#{milestone_number})"
        if milestone_title
        else f"#{milestone_number}"
    )
    click.echo(f"#{issue} ({repo_entry.github}) assigned to milestone {ms_label}")


@milestone_group.command(
    "remove",
    help=(
        "Unassign an issue from its milestone (#1003) — the counterpart to "
        "`coord milestone assign`. REPO is the local repo name from "
        "coordinator.yml; ISSUE is the GH issue number. Idempotent — "
        "clearing an issue that has no milestone is a no-op. Routes through "
        "the daemon seam (``POST /issue-milestone-remove``) so the local "
        "issues cache ``milestone_number``/``milestone_title`` columns are "
        "cleared immediately (no need to wait for ``coord sync``)."
    ),
)
@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def milestone_remove_cmd(
    repo: str,
    issue: int,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    from coord.state import unassign_issue_milestone  # noqa: PLC0415

    try:
        unassign_issue_milestone(repo, issue, repo_github=repo_entry.github)
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: milestone remove failed: {e}", err=True)
        sys.exit(1)

    click.echo(f"#{issue} ({repo_entry.github}) removed from its milestone")


@milestone_group.command(
    "add-child",
    help=(
        "Idempotent append/remove on an epic tracking issue's `## "
        "Sub-issues` checklist (#1008) — same splice-not-duplicate spirit "
        "as `coord milestone write-order` for `## Work order`, keyed on a "
        "different heading. REPO is the local repo name from "
        "coordinator.yml; EPIC is the GH issue number of the epic tracking "
        "issue; ISSUE is the child issue number to add (or remove, with "
        "--remove).\n\n"
        "Adding an ISSUE already present with identical --group/--after "
        "is a no-op; adding it again with different annotations updates "
        "that line in place (preserving its `[x]` checked state). "
        "--remove drops ISSUE from the checklist (no-op if absent) and "
        "cannot be combined with --group/--after. The resulting checklist "
        "is re-parsed and validated (no duplicate/undeclared-`after`/cycle) "
        "before writing via `github_ops.update_issue_body` — the epic's "
        "`## Work order` section (if any) is left untouched."
    ),
)
@click.argument("repo")
@click.argument("epic", type=int)
@click.argument("issue", type=int)
@click.option(
    "--group", default=None, help="Optional `{group: G}` annotation for ISSUE."
)
@click.option(
    "--after", "after_raw", default=None,
    help="Optional `{after: N,...}` annotation — comma-separated issue numbers.",
)
@click.option(
    "--remove", is_flag=True,
    help="Remove ISSUE from the epic's `## Sub-issues` checklist instead of adding it.",
)
@_CONFIG_OPTION
def milestone_add_child_cmd(
    repo: str,
    epic: int,
    issue: int,
    group: str | None,
    after_raw: str | None,
    remove: bool,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    if remove and (group is not None or after_raw is not None):
        click.echo("error: --remove cannot be combined with --group/--after", err=True)
        sys.exit(2)

    after: tuple[int, ...] = ()
    if after_raw:
        try:
            after = tuple(
                int(chunk.strip().lstrip("#"))
                for chunk in after_raw.split(",")
                if chunk.strip()
            )
        except ValueError:
            click.echo(
                f"error: --after must be a comma-separated list of issue "
                f"numbers, got {after_raw!r}",
                err=True,
            )
            sys.exit(2)

    from coord import github_ops  # noqa: PLC0415

    try:
        epic_data = github_ops.get_issue(repo_entry.github, epic)
    except RuntimeError as e:
        click.echo(f"error: could not fetch epic #{epic}: {e}", err=True)
        sys.exit(1)

    if not remove:
        # Catch a typo'd child issue number before it's baked into the
        # epic's body — mirrors write-order's pre-write validation rigor.
        try:
            github_ops.get_issue(repo_entry.github, issue)
        except RuntimeError as e:
            click.echo(f"error: could not fetch issue #{issue}: {e}", err=True)
            sys.exit(1)

    old_body = epic_data.get("body") or ""
    try:
        current = parse_sub_issues(old_body)
    except WorkOrderError as e:
        click.echo(f"error: existing `## Sub-issues` block is invalid: {e}", err=True)
        sys.exit(1)

    nodes = list(current.nodes)
    existing_idx = next(
        (i for i, n in enumerate(nodes) if n.issue_number == issue), None
    )

    if remove:
        if existing_idx is None:
            click.echo(
                f"#{epic} ({repo_entry.github}): #{issue} is not in the "
                "`## Sub-issues` checklist (no-op)"
            )
            return
        nodes.pop(existing_idx)
    else:
        prior_checked = (
            nodes[existing_idx].checked if existing_idx is not None else False
        )
        candidate_node = WorkOrderNode(
            issue_number=issue, group=group, after=after, checked=prior_checked
        )
        if existing_idx is not None:
            if nodes[existing_idx] == candidate_node:
                click.echo(
                    f"#{epic} ({repo_entry.github}): #{issue} already in the "
                    "`## Sub-issues` checklist (no-op)"
                )
                return
            nodes[existing_idx] = candidate_node
        else:
            nodes.append(candidate_node)

    new_block = render_sub_issues(WorkOrder(nodes=tuple(nodes)))
    candidate_body = replace_sub_issues_section(old_body, new_block)

    try:
        parse_sub_issues(candidate_body)
    except WorkOrderError as e:
        click.echo(
            f"error: resulting `## Sub-issues` block would be invalid: {e}", err=True
        )
        sys.exit(1)

    if candidate_body == old_body:
        click.echo(
            f"#{epic} ({repo_entry.github}): `## Sub-issues` unchanged "
            "(idempotent no-op)"
        )
        return

    github_ops.update_issue_body(repo_entry.github, epic, candidate_body)
    action = "removed from" if remove else "added to"
    click.echo(
        f"#{issue} {action} #{epic}'s ({repo_entry.github}) `## Sub-issues` checklist"
    )


_DEFAULT_CAPTURE_BODY = (
    "Captured via coord-tui fast plan capture — no work order yet. "
    "Promote to a full epic with `coord milestone chat`."
)


@milestone_group.command(
    "capture",
    help=(
        "Fast-capture a lightweight plan stub (#977): creates a new milestone "
        "titled TEXT, a plain issue (no `epic` label) under it, and assigns "
        "the issue to the milestone — all through the existing write seams "
        "(`coord milestone create` + `coord issue create` + `coord milestone "
        "assign` composed in one step). REPO is the local repo name from "
        "coordinator.yml. Appears immediately in the `coord plans` / TUI "
        "Plans-panel roster flagged `no_work_order`, ready to be promoted to "
        "a full epic later via `coord milestone chat`."
    ),
)
@click.argument("repo")
@click.option(
    "--title",
    required=True,
    help="Plan title (used for both the milestone and the issue).",
)
@click.option(
    "--body",
    default=None,
    help="Issue body (markdown). Defaults to a short note pointing at `coord milestone chat`.",
)
@_CONFIG_OPTION
def milestone_capture_cmd(
    repo: str,
    title: str,
    body: str | None,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)
    slug = repo_entry.github

    from coord.state import assign_issue_milestone, create_issue, write_milestone  # noqa: PLC0415

    try:
        ms = write_milestone(repo, title=title, repo_github=slug)
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: plan capture failed creating milestone: {e}", err=True)
        sys.exit(1)

    try:
        issue = create_issue(repo, title, body or _DEFAULT_CAPTURE_BODY, repo_github=slug)
    except Exception as e:  # noqa: BLE001
        click.echo(
            f"error: plan capture failed creating issue (milestone #{ms.get('number')} "
            f"was already created): {e}",
            err=True,
        )
        sys.exit(1)

    try:
        assign_issue_milestone(
            repo,
            issue["number"],
            ms["number"],
            milestone_title=title,
            repo_github=slug,
        )
    except Exception as e:  # noqa: BLE001
        click.echo(
            f"error: plan capture failed assigning issue #{issue.get('number')} to "
            f"milestone #{ms.get('number')}: {e}",
            err=True,
        )
        sys.exit(1)

    click.echo(
        f"plan captured: milestone #{ms['number']} ({slug}) — issue #{issue['number']} "
        "— no work order yet. Promote via `coord milestone chat`."
    )


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
@click.option(
    "--pick", "pick_issue", type=int, default=None,
    help=(
        "#1003: non-interactive companion to --next — dispatch this "
        "specific ready-frontier issue number without the interactive "
        "prompt (the coord-tui Plans-panel/MilestoneDag \"Dispatch next…\" "
        "action's backend; a bare TTY-less `--next` would otherwise hang "
        "on `click.prompt`). Errors if the issue isn't currently in the "
        "ready-to-dispatch frontier."
    ),
)
@_CONFIG_OPTION
def milestone_dispatch_cmd(
    repo: str,
    tracking_issue: int,
    dry_run: bool,
    next_: bool,
    pick_issue: int | None,
    config_path: Path,
) -> None:
    if pick_issue is not None and not next_:
        click.echo("error: --pick requires --next", err=True)
        sys.exit(2)
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

    # Gate A (#930, docs/ORACLE_LOOP.md): refuse to dispatch any of this
    # milestone's issues until its black-box contract exists. Checked before
    # everything else — including `--dry-run`/`--next` — so the gate is
    # never silently bypassable.
    block_reason = gate_a_status(repo_entry, cfg, ctx.milestone_number)
    if block_reason:
        click.echo(f"error: {block_reason}", err=True)
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

        # #1003: --pick short-circuits the interactive prompt entirely — the
        # operator (or, via coord-tui, a client that already knows the
        # issue number it wants) names the issue directly instead of
        # picking an index off a numbered list. Search the FULL
        # to-dispatch set, not just the top-3 `choices` shown to a human —
        # a non-interactive caller has no reason to be limited to the
        # truncated display.
        if pick_issue is not None:
            chosen = next(
                (p for p in plan.to_dispatch if p.entry.issue_number == pick_issue),
                None,
            )
            if chosen is None:
                click.echo(
                    f"error: #{pick_issue} is not in the ready-to-dispatch "
                    "frontier right now",
                    err=True,
                )
                sys.exit(1)
            if dry_run:
                click.echo(
                    f"(dry run — would dispatch #{pick_issue} -> {chosen.machine.name})"
                )
                return
            outcome = dispatch_entry(
                chosen, repo_entry, cfg, board, tracking_issue=tracking_issue
            )
            _echo_outcome(outcome)
            if not outcome.ok:
                sys.exit(1)
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


@milestone_group.command(
    "gate-c",
    help=(
        "Gate C (docs/ORACLE_LOOP.md, #932): the milestone's FULL "
        "accumulated acceptance suite must be green before it ships — "
        "catches the integration gaps *between* issues that per-issue "
        "acceptance runs miss. Runs the repo's acceptance driver once "
        "against the current checkout (`--path`, default: the repo's "
        "configured local checkout) and reports pass/fail with a "
        "non-zero exit on red, alongside a per-issue rollup of the "
        "milestone's own Acceptance box state (e.g. \"3/7 acceptance "
        "green\") for visibility. This is a manual check the operator "
        "runs before treating a milestone as done — no `feature/ms-NN "
        "→ develop` ship-path automation exists yet (#933/#934), so "
        "nothing here mutates git state or blocks anything automatically."
    ),
)
@click.argument("repo")
@click.argument("tracking_issue", type=int)
@click.option(
    "--path", "path_opt", type=click.Path(file_okay=False), default=None,
    help="Repo checkout to run the driver in (default: repo_paths in coordinator.yml).",
)
@_CONFIG_OPTION
def milestone_gate_c_cmd(
    repo: str, tracking_issue: int, path_opt: str | None, config_path: Path
) -> None:
    from coord.acceptance import build_verdict
    from coord.acceptance_drivers import DriverError, run_driver

    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    driver_cfg = cfg.acceptance.driver_for(repo)
    if driver_cfg is None:
        click.echo(
            f"error: no acceptance driver configured for repo {repo!r} "
            "(add it under acceptance.drivers in coordinator.yml)",
            err=True,
        )
        sys.exit(1)

    if path_opt is not None:
        repo_dir = Path(path_opt).expanduser()
    else:
        from coord.test_orchestrator import find_local_repo_path

        found = find_local_repo_path(repo, cfg)
        if found is None:
            click.echo(
                f"error: no local repo checkout found for {repo!r} "
                "(repo_paths in coordinator.yml) — pass --path",
                err=True,
            )
            sys.exit(1)
        repo_dir = found

    try:
        ctx = fetch_milestone_context(repo_entry, tracking_issue)
    except MilestoneDispatchError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    click.echo(
        f"Gate C for #{tracking_issue} (milestone #{ctx.milestone_number}) "
        f"— running the full accumulated acceptance suite in {repo_dir}..."
    )
    try:
        result = run_driver(driver_cfg.kind, driver_cfg.run, cwd=str(repo_dir))
    except DriverError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    if not result.tests and result.exit_code != 0:
        click.echo(result.raw_output, err=True)

    verdict = build_verdict(result.tests, scope="all")
    click.echo(json.dumps(verdict, indent=2))

    # Per-issue rollup: how many of the milestone's own issues already have
    # a passed Acceptance box, for visibility alongside the full-suite
    # verdict (docs/ORACLE_LOOP.md's "3/7 acceptance green" example is a
    # per-milestone-member reading, not this full-suite one).
    if ctx.work_order.nodes:
        from coord import board_service
        from coord.diagnose import stage_assignments

        board = board_service.read_board()
        passed = 0
        with_signal = 0
        for node in ctx.work_order.nodes:
            work = stage_assignments(board, repo, node.issue_number, "work")
            with_state = [a for a in work if (a.acceptance_state or "") != ""]
            if not with_state:
                continue
            with_signal += 1
            if with_state[0].acceptance_state == "passed":
                passed += 1
        click.echo(
            f"\nMilestone Acceptance boxes: {passed}/{with_signal} passed "
            f"({len(ctx.work_order.nodes)} issue(s) total, "
            f"{len(ctx.work_order.nodes) - with_signal} with no verdict yet)"
        )

    if verdict["total"] == 0 or not verdict["green"]:
        click.echo(f"\nGate C RED for #{tracking_issue} — milestone is not ready to ship.")
        sys.exit(1)
    click.echo(f"\nGate C GREEN for #{tracking_issue}.")
