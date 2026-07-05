"""`coord plans` — read-only milestone-roster aggregation (#974).

Fetches every open GitHub milestone across all configured repos (or a single
repo when ``--repo`` is given), finds each milestone's tracking epic (the
``"epic"``-labelled issue), parses its ``## Work order`` block, and emits a
JSON array (``--json``) or a human-readable table of plan stats + attention
signals.

This command is **read-only**: it never writes issues, comments, or the board.
The JSON output is the data backbone consumed by the TUI "Plans" panel (#975).

Attention signals (``needs_you`` field):

``no_work_order``
    Milestone has no epic with a ``## Work order`` block — someone needs to
    write one via ``coord milestone chat`` / ``coord milestone write-order``.
``ready_waiting``
    ≥1 ready-frontier entry exists.  Use ``coord milestone dispatch`` to kick
    it off, or ``coord milestone order`` to review the frontier first.
``stalled``
    Has a work order, nothing is ready or in-flight, and the milestone is not
    done.  A dependency is blocking everything and may need attention.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from coord.commands._common import _CONFIG_OPTION, _load_config
from coord.plans import aggregate_repo_plans


@click.command(
    "plans",
    help=(
        "Read-only milestone-roster aggregation (#974). "
        "Fetches every open milestone across all configured repos (or just REPO "
        "when --repo is given), parses each tracking epic's `## Work order` "
        "block, and reports per-plan stats + attention signals.\n\n"
        "Primary output is --json (a JSON array of plan objects). "
        "Without --json a compact human-readable table is printed instead."
    ),
)
@click.option(
    "--repo",
    default=None,
    metavar="REPO",
    help=(
        "Restrict to this coord-local repo name "
        "(from coordinator.yml). Default: all repos."
    ),
)
@click.option(
    "--json",
    "json_out",
    is_flag=True,
    help="Emit machine-readable JSON (array of plan objects).",
)
@_CONFIG_OPTION
def plans_cmd(repo: str | None, json_out: bool, config_path: Path) -> None:
    from coord import board_service, github_ops  # noqa: PLC0415

    cfg = _load_config(config_path)

    # Resolve the target repos.
    if repo is not None:
        repo_entry = cfg.repo(repo)
        if repo_entry is None:
            click.echo(f"error: unknown repo {repo!r} (not in coordinator.yml)", err=True)
            sys.exit(2)
        target_repos = [repo_entry]
    else:
        target_repos = list(cfg.repos)

    board = board_service.read_board()

    all_entries = []
    errors: list[str] = []

    for repo_entry in target_repos:
        try:
            milestones = github_ops.get_repo_milestones(repo_entry.github)
        except RuntimeError as e:
            errors.append(f"warning: could not list milestones for {repo_entry.github}: {e}")
            continue

        if not milestones:
            continue

        try:
            open_issues = github_ops.get_open_issues(repo_entry.github)
        except RuntimeError as e:
            errors.append(f"warning: could not fetch issues for {repo_entry.github}: {e}")
            continue

        entries = aggregate_repo_plans(
            repo_name=repo_entry.name,
            repo_github=repo_entry.github,
            milestones=milestones,
            open_issues=open_issues,
            board=board,
        )
        all_entries.extend(entries)

    # Emit warnings regardless of output mode.
    for msg in errors:
        click.echo(msg, err=True)

    if json_out:
        click.echo(json.dumps([e.to_dict() for e in all_entries], indent=2))
        return

    # Human-readable table.
    if not all_entries:
        click.echo("No open milestones found.")
        return

    for entry in all_entries:
        status_parts: list[str] = []
        if entry.has_work_order:
            status_parts.append(
                f"ready={entry.ready_frontier} "
                f"in-flight={entry.in_flight} "
                f"blocked={entry.blocked} "
                f"done={entry.done}/{entry.total}"
            )
        else:
            status_parts.append("no work order")

        if entry.needs_you:
            status_parts.append(f"[{', '.join(entry.needs_you)}]")

        tracking = f"#{entry.tracking_issue}" if entry.tracking_issue else "—"
        click.echo(
            f"{entry.repo}  #{entry.milestone_number}  {entry.title!r}  "
            f"epic:{tracking}  {' '.join(status_parts)}"
        )
