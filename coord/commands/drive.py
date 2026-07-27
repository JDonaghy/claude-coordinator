"""``coord drive`` — the unattended single-issue driver (#1392).

Thin Click wrapper: parse flags into :class:`~coord.drive.DriveOptions`, build
the :class:`~coord.drive.Driver`, translate :class:`~coord.drive.DriveError`
into a clean exit code.  Every decision lives in ``coord/drive.py``.

Replaces ``scripts/drive-issue.sh`` + ``scripts/coord_issue_state.py``, deleted
in the same change (no two implementations).  ``scripts/coord-test-runner.sh``
deliberately stays as shell — it is genuinely subprocess/venv/cargo-env work,
and its four sharp-edged parsers already live in ``coord/test_report.py``.
"""

from __future__ import annotations

from pathlib import Path

import click

from coord.commands._common import _CONFIG_OPTION, _load_config


_DRIVE_HELP = """Drive ONE issue Work → Test → Review → Merge, unattended.

A resumable state machine over the daemon's board: it dispatches the WORK
assignment, then OBSERVES Test/Review/Merge (coord dispatches all three
itself), looping a failing test through `coord fix` on the same branch.
Re-running it on the same issue is safe and resumes from wherever the board
actually is.

Exit codes: 0 merged (verified against the remote default branch), or review
approved with --no-merge; 1 a stage reached a terminal failure a script cannot
resolve; 2 bad usage / configuration; 3 deadline exceeded.
"""


@click.command("drive", help=_DRIVE_HELP)
@click.argument("repo")
@click.argument("issue", type=int)
@click.option(
    "--machine",
    default="",
    help=(
        "Machine for the work dispatch (default: least-loaded unpaused machine "
        "that hosts the repo)."
    ),
)
@click.option(
    "--model",
    default="",
    help="Model tier (haiku|sonnet|opus). Default: models.default.",
)
@click.option(
    "--briefing-file",
    default="",
    type=click.Path(),
    help=(
        "REPLACES the entire auto-generated briefing for the work dispatch — it "
        "is NOT an addendum. The issue body, project rules and file scope are "
        "all dropped, so the worker only ever sees this file. To ADD guidance "
        "while keeping the real briefing, use `coord context add --pin <repo> "
        "<issue> '<note>'`, which prepends to every briefing (#603)."
    ),
)
@click.option(
    "--plan",
    "do_plan",
    is_flag=True,
    help=(
        "Run a read-only plan stage first and auto-approve it "
        "(coord assign --plan-only → coord approve-plan)."
    ),
)
@click.option(
    "--max-fix-rounds",
    default=3,
    show_default=True,
    help=(
        "Headless `coord fix` rounds on a failing test suite. Each round "
        "continues the SAME branch with the model escalated (sonnet → opus). "
        "coord dispatches the Test stage itself onto a capability-matched "
        "machine (#1426); this only observes the verdict."
    ),
)
@click.option(
    "--skip-test",
    is_flag=True,
    help=(
        "Record the Test gate as `skipped` directly (via `coord test --skipped`) "
        "— no dispatch. Use only for genuinely untestable diffs."
    ),
)
@click.option(
    "--repo-path",
    default="",
    type=click.Path(),
    help=(
        "Local checkout used for branch/merge verification (git fetch + "
        "cherry against origin). Default: ~/src/<repo>."
    ),
)
@click.option("--poll", default=60.0, show_default=True, help="Board poll interval (s).")
@click.option(
    "--max-work-retries",
    default=1,
    show_default=True,
    help="`coord retry` attempts on a failed work stage.",
)
@click.option(
    "--deadline",
    "deadline_mins",
    default=240.0,
    show_default=True,
    help="Give up after this long (minutes).",
)
@click.option(
    "--stall",
    "stall_mins",
    default=20.0,
    show_default=True,
    help="Warn (and nudge, with --notify) after this long with no state change.",
)
@click.option(
    "--notify",
    is_flag=True,
    help=(
        "Also run `coord notify` under ~/.coord/notify.lock when stalled, to cut "
        "the 5-minute timer latency. OFF by default: two drivers racing to "
        "dispatch the same thing is the #476/#477 duplicate-fix-worker incident."
    ),
)
@click.option(
    "--accept-advisory",
    is_flag=True,
    help=(
        "Proceed when the work row is ADVISORY but its branch demonstrably "
        "carries commits. Needed while #1357 is open."
    ),
)
@click.option(
    "--force-review",
    is_flag=True,
    help=(
        "Explicitly request the review for work completed in an INTERACTIVE "
        "session. coord's #555 guard never auto-dispatches one for "
        "provider=claude-pty, so without this the run stops at preflight "
        "rather than stalling forever."
    ),
)
@click.option("--no-merge", is_flag=True, help="Stop after the review approves.")
@click.option(
    "--merge-method",
    type=click.Choice(["rebase", "squash", "merge"]),
    default="rebase",
    show_default=True,
)
@click.option(
    "--max-merge-attempts",
    default=3,
    show_default=True,
    help="`coord merge` attempts before giving up (bounds a merge the board never reflects).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the resolved plan and current state, then exit.",
)
@_CONFIG_OPTION
def drive(
    repo: str,
    issue: int,
    machine: str,
    model: str,
    briefing_file: str,
    do_plan: bool,
    max_fix_rounds: int,
    skip_test: bool,
    repo_path: str,
    poll: float,
    max_work_retries: int,
    deadline_mins: float,
    stall_mins: float,
    notify: bool,
    accept_advisory: bool,
    force_review: bool,
    no_merge: bool,
    merge_method: str,
    max_merge_attempts: int,
    dry_run: bool,
    config_path: Path,
) -> None:
    # Imported lazily so `coord --help` doesn't pay for httpx/subprocess setup.
    from coord.drive import Driver, DriveError, DriveOptions  # noqa: PLC0415

    config = _load_config(config_path)
    opts = DriveOptions(
        machine=machine,
        model=model,
        briefing_file=briefing_file,
        do_plan=do_plan,
        max_fix_rounds=max_fix_rounds,
        skip_test=skip_test,
        repo_path=repo_path,
        poll=poll,
        max_work_retries=max_work_retries,
        deadline_mins=deadline_mins,
        stall_mins=stall_mins,
        notify=notify,
        do_merge=not no_merge,
        merge_method=merge_method,
        accept_advisory=accept_advisory,
        force_review=force_review,
        dry_run=dry_run,
        max_merge_attempts=max_merge_attempts,
        config_path=str(config_path) if config_path else "",
    )
    driver = Driver(repo=repo, issue=issue, opts=opts, config=config)
    try:
        code = driver.run()
    except DriveError as exc:
        click.echo(f"✗ {exc}", err=True)
        raise SystemExit(exc.exit_code) from None
    except KeyboardInterrupt:
        click.echo("interrupted", err=True)
        raise SystemExit(130) from None
    raise SystemExit(code)
