"""Plan-mode follow-up commands: `pr`, `fix`, `approve-plan`,
`reject-plan`, `resume-stuck`, `split`. Extracted from coord/cli.py (#747)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import click
import httpx

from coord.config import Config

from coord.commands._common import AGENT_PORT, _CONFIG_OPTION, _load_config


@click.command(help="Create sub-issues from a split proposal (e.g. coord split S1).")
@click.argument("ids")
@_CONFIG_OPTION
@click.option("--dry-run", is_flag=True, help="Show what would be created.")
def split(ids: str, config_path: Path, dry_run: bool) -> None:
    from coord import github_ops
    from coord.state import load_split_proposals, clear_split_proposals

    cfg = _load_config(config_path)
    splits = load_split_proposals()
    if not splits:
        click.echo("No pending split proposals. Run `coord plan` first.", err=True)
        sys.exit(1)

    try:
        selected_ids = [int(x.strip().lstrip("Ss")) for x in ids.split(",")]
    except ValueError:
        click.echo("error: IDs must be comma-separated (e.g. S1,S2 or 1,2)", err=True)
        sys.exit(2)

    selected = [s for s in splits if s.id in selected_ids]
    missing = set(selected_ids) - {s.id for s in selected}
    if missing:
        click.echo(f"error: unknown split proposal IDs: {missing}", err=True)
        sys.exit(2)

    for s in selected:
        repo = cfg.repo(s.repo_name)
        if repo is None:
            click.echo(f"error: unknown repo {s.repo_name!r}", err=True)
            continue

        click.echo(f"\nSplitting #{s.issue_number}: {s.issue_title} into {len(s.chunks)} sub-issues:")

        child_numbers: list[int] = []
        for j, chunk in enumerate(s.chunks, 1):
            title = f"{chunk.title} (sub-task {j}/{len(s.chunks)} of #{s.issue_number})"
            body = (
                f"## Sub-task of #{s.issue_number} — {s.issue_title}\n\n"
                f"### Scope (chunk {j} of {len(s.chunks)}): {chunk.title}\n\n"
                f"{chunk.scope}\n\n"
                f"### Files likely touched\n\n"
                + "\n".join(f"- `{f}`" for f in chunk.files_likely)
                + f"\n\n### Context\n\n- Parent issue: #{s.issue_number}\n"
            )

            if dry_run:
                click.echo(f"  [{j}] would create: {title}")
                continue

            try:
                result = github_ops.create_issue(
                    repo.github, title, body, labels=["sub-task"],
                )
                child_numbers.append(result["number"])
                click.echo(f"  [{j}] created #{result['number']}: {chunk.title}")
            except RuntimeError as e:
                click.echo(f"  [{j}] failed to create: {e}", err=True)

        if dry_run or not child_numbers:
            continue

        task_list = "\n".join(
            f"- [ ] #{n}" for n in child_numbers
        )
        try:
            github_ops.update_issue_body(
                repo.github, s.issue_number,
                f"Split into sub-tasks:\n\n{task_list}\n",
            )
            click.echo(f"  Parent #{s.issue_number} updated with task list")
        except RuntimeError as e:
            click.echo(f"  Failed to update parent: {e}", err=True)

    if not dry_run:
        clear_split_proposals()
        click.echo("\nSplit proposals cleared. Run `coord plan` to assign the new sub-issues.")


def _dispatch_followup(
    cfg: Config,
    original: Assignment,
    briefing: str,
    *,
    issue_suffix: str = "",
    model: str | None = None,
    type: str = "work",
    files_likely: list[str] | None = None,
    inherit_branch: bool = True,
) -> str:
    """Dispatch a follow-up assignment for an existing assignment. Returns assignment ID.

    *model* overrides the model tier for the follow-up. When None, the
    dispatcher falls back to ``cfg.models.default``.

    *type* sets the assignment type (``"work"`` or ``"plan"``).  Defaults to
    ``"work"`` so existing callers are unaffected.

    *files_likely* is the list of files the worker is expected to touch.
    When None, an empty list is used (no file constraints).

    *inherit_branch* controls whether the follow-up checks out the parent's
    branch (``target_branch=original.branch``).  True for follow-ups that
    *continue* existing work on the same branch (``coord pr``, smoke-test
    fix-up, continuation).  Must be False when the parent is a read-only
    PLAN assignment: a plan never pushes, its recorded branch is a
    throwaway worktree name (sometimes a stale/wrong capture), and the
    work it spawns must start a FRESH branch derived from the issue.
    """
    from coord.board_service import read_board, write_board
    from coord.dispatch import dispatch, post_briefing, compute_do_not_touch
    from coord.state import record_dispatched
    from coord.models import Proposal

    repo = cfg.repo(original.repo_name)
    if repo is None:
        raise ValueError(f"Unknown repo: {original.repo_name!r}")

    proposal = Proposal(
        id=0,
        machine_name=original.machine_name,
        repo_name=original.repo_name,
        issue_number=original.issue_number,
        issue_title=original.issue_title,
        rationale=f"follow-up for assignment {original.assignment_id}",
        briefing=briefing,
        model=model if model else cfg.models.default,
        type=type,
        files_likely=files_likely if files_likely is not None else [],
        # Pin the follow-up to the parent's branch when one exists AND the
        # caller wants continuation.  Without this, prefixed issue titles
        # like `[fix-1] …` / `[conflict-fix] …` carried into
        # _dispatch_followup (e.g. `coord pr` on a fix-up assignment)
        # cause the agent to slugify the prefixed title and push to an
        # orphan branch instead of the original PR's branch.  But for a
        # plan→work hand-off the parent is read-only and its branch is a
        # throwaway (sometimes wrong) capture, so the work must branch
        # fresh — callers pass inherit_branch=False there.
        target_branch=(original.branch or None) if inherit_branch else None,
    )

    response = dispatch(proposal, cfg)
    assignment_id = response.get("id", "pending")
    record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github=repo.github,
        provider_name=response.get("_provider_name"),
    )

    # #906: use read_board() to build the peer-conflict in-flight list instead
    # of load_dispatched() — the latter reads the local DB which is empty on a
    # thin client.  read_board() routes to the daemon's /board when configured,
    # so we get the canonical active assignment list for do-not-touch detection.
    # This also consolidates the board read we needed anyway for write_board().
    board = read_board()
    in_flight = [
        {
            "machine_name": a.machine_name,
            "repo_name": a.repo_name,
            "files_likely": a.files_allowed,
        }
        for a in board.active
        if a.assignment_id != assignment_id  # exclude just-dispatched
    ]
    do_not_touch = compute_do_not_touch(proposal, peers=[], in_flight=in_flight)
    post_briefing(proposal, cfg, assignment_id=assignment_id, do_not_touch=do_not_touch)

    write_board(board)

    return assignment_id


def _load_plan_for_assignment(assignment, assignment_id: str) -> dict | None:
    """Retrieve the plan dict for a plan-type assignment.

    Tries (in order):
    1. The plan field cached on the assignment object.
    2. The plans table in the DB (populated by `coord notify`).
    3. Parsing the local log file directly (works when agent is local).

    Returns the plan dict or None if not found.
    """
    from coord.state import COORD_DIR, load_plans

    plan_dict = getattr(assignment, "plan", None)
    if plan_dict is None:
        plans = load_plans()
        plan_dict = plans.get(assignment_id)
    if plan_dict is None:
        local_log = COORD_DIR / "logs" / f"{assignment_id}.log"
        try:
            from coord.plan_parser import parse_plan_from_log  # noqa: PLC0415
            worker_plan = parse_plan_from_log(local_log)
        except Exception:  # noqa: BLE001
            worker_plan = None
        if worker_plan is not None:
            plan_dict = worker_plan.to_dict()
    return plan_dict


def _plan_dict_to_text(plan_dict: dict) -> str:
    """Format a WorkerPlan dict into a human-readable text block for briefings."""
    from coord.plan_parser import WorkerPlan  # noqa: PLC0415

    plan = WorkerPlan.from_dict(plan_dict)
    parts: list[str] = []
    if plan.plan:
        parts.append(f"Summary:\n{plan.plan}")
    if plan.files_modify:
        parts.append("Files to modify:\n" + "\n".join(f"  - {f}" for f in plan.files_modify))
    if plan.approach:
        parts.append(f"Approach:\n{plan.approach}")
    if plan.risks:
        parts.append(f"Risks:\n{plan.risks}")
    if plan.estimate:
        parts.append(f"Estimate:\n{plan.estimate}")
    # Smoke tests authored at planning time — the work worker re-emits
    # these (refining if needed) in its own SMOKE_TESTS block before
    # exit.  Surfacing them in the briefing lets the worker copy them
    # verbatim when the change matches the plan.
    if plan.smoke_tests:
        bullets = "\n".join(f"  - {b}" for b in plan.smoke_tests)
        parts.append(f"Smoke tests (from plan — re-emit in your SMOKE_TESTS block):\n{bullets}")
    elif plan.smoke_tests == []:
        parts.append(
            "Smoke tests (from plan): (none — change is internal). "
            "Emit `SMOKE_TESTS: (none — change is internal)` in your block."
        )
    # Fall back to raw_text when no structured sections were found.
    if not parts:
        return plan.raw_text or "(no plan text)"
    return "\n\n".join(parts)


@click.command(help="Dispatch a worker to create a PR for a completed assignment.")
@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option(
    "--no-review",
    is_flag=True,
    default=False,
    help="Skip auto-dispatching an adversarial review after the PR worker.",
)


def pr(assignment_id: str, config_path: Path, no_review: bool) -> None:
    from coord.board_service import read_board, write_board

    cfg = _load_config(config_path)
    board = read_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    if assignment.status != "done":
        click.echo(
            f"error: assignment {assignment_id} is {assignment.status!r}, "
            "can only create a PR for done assignments",
            err=True,
        )
        sys.exit(1)

    if not assignment.branch:
        click.echo(
            f"error: assignment {assignment_id} has no branch recorded. "
            "The worker may not have pushed yet.",
            err=True,
        )
        sys.exit(1)

    repo = cfg.repo(assignment.repo_name)
    if repo is None:
        click.echo(f"error: unknown repo {assignment.repo_name!r}", err=True)
        sys.exit(1)

    default_branch = repo.default_branch
    # #1077: "mock-author" (Gate A) assignments' issue_number is the
    # milestone's tracking issue, not something this PR resolves — closing
    # it on merge would wrongly flip the epic to "done" while its real
    # sub-issues are untouched. Only "work"-type PRs get the closing
    # keyword; everything else gets a non-closing reference.
    from coord.models import CLOSES_ISSUE_TYPES, PR_HELPER_TYPE  # noqa: PLC0415

    closes_issue = assignment.type in CLOSES_ISSUE_TYPES
    ref_keyword = (
        f"Closes #{assignment.issue_number}"
        if closes_issue
        else f"Refs #{assignment.issue_number}"
    )
    briefing = (
        f"You are on branch {assignment.branch}. The code is complete and tests pass.\n"
        f"Create a PR from {assignment.branch} to {default_branch} for issue #{assignment.issue_number}.\n"
        f"Title: {assignment.issue_title}\n\n"
        f"Use gh pr create. Read the diff (git fetch origin && git diff origin/{default_branch}...HEAD) and write a clear\n"
        f"summary of what changed. Reference the issue with \"{ref_keyword}\".\n"
        f"Do NOT modify any code — only create the PR."
    )

    # #1142: only give the PR-opening helper `type="work"` when the original
    # assignment's own type actually resolves `issue_number` (mirrors the
    # Closes/Refs split above). Otherwise (test-author/mock-author/etc, whose
    # issue_number is a milestone tracking issue) the helper gets a distinct
    # `PR_HELPER_TYPE` so it can never be mistaken for that tracking issue's
    # own merged work by `coord.stage_projection.merge_stage_status_for` (or
    # any other heuristic keyed on `type == "work"` / `CLOSES_ISSUE_TYPES`).
    followup_type = "work" if closes_issue else PR_HELPER_TYPE

    try:
        new_id = _dispatch_followup(cfg, assignment, briefing, type=followup_type)
    except httpx.HTTPError as e:
        click.echo(f"error: dispatch failed: {e}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    click.echo(f"PR worker dispatched (assignment {new_id})")
    click.echo(f"  branch: {assignment.branch} → {default_branch}")
    click.echo(f"  issue: #{assignment.issue_number}: {assignment.issue_title}")

    if not no_review and cfg.reviews.enabled:
        from coord.review import dispatch_review

        fresh_board = read_board()
        review = dispatch_review(assignment, fresh_board, cfg)
        if review is not None:
            write_board(fresh_board)
            click.echo(f"Review dispatched (assignment {review.assignment_id})")
            click.echo(f"  reviewer: {review.machine_name}")
        else:
            click.echo("  review not dispatched (no eligible machine or reviews disabled)")


@click.command(help="Dispatch a fix-up worker for a failed smoke test.")
@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option("--guidance", default="", help="Additional guidance for the fix-up worker.")
def fix(assignment_id: str, config_path: Path, guidance: str) -> None:
    from coord.board_service import read_board
    from coord.state import COORD_DIR

    cfg = _load_config(config_path)
    board = read_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    if assignment.smoke_test != "fail":
        click.echo(
            f"error: assignment {assignment_id} smoke_test is "
            f"{assignment.smoke_test!r}, expected 'fail'",
            err=True,
        )
        sys.exit(1)

    repo = cfg.repo(assignment.repo_name)
    if repo is None:
        click.echo(f"error: unknown repo {assignment.repo_name!r}", err=True)
        sys.exit(1)

    default_branch = repo.default_branch

    # Load stored test output if available
    test_output = ""
    test_output_file = COORD_DIR / "test_output" / f"{assignment_id}.txt"
    if test_output_file.exists():
        test_output = test_output_file.read_text()
    elif assignment.smoke_test_reason:
        test_output = assignment.smoke_test_reason

    guidance_text = guidance or "Fix the failing tests and push."

    briefing = (
        f"You are fixing a failed smoke test for issue #{assignment.issue_number}: {assignment.issue_title}\n\n"
        f"The previous worker created branch {assignment.branch}. You are already on that branch.\n"
        f"Do NOT start over — work from the existing code.\n\n"
        f"## What was done\n"
        f"The previous worker's changes are already committed on this branch.\n"
        f"Run `git fetch origin && git log --oneline origin/{default_branch}..HEAD` to see what was done.\n"
        f"Run `git diff origin/{default_branch}...HEAD` to see the full diff.\n\n"
        f"## Test failure\n"
        f"{test_output}\n\n"
        f"## Guidance\n"
        f"{guidance_text}\n\n"
        f"## Rules\n"
        f"- Do NOT start over or rewrite from scratch\n"
        f"- Fix the specific test failures\n"
        f"- Commit your fixes and push with git push origin HEAD"
    )

    # Determine escalated model for the fix-up.
    original_model = assignment.model or cfg.models.default
    escalated = cfg.models.next_model(original_model)
    if escalated != original_model:
        click.echo(f"  escalating model: {original_model} → {escalated}")

    try:
        new_id = _dispatch_followup(cfg, assignment, briefing, model=escalated)
    except httpx.HTTPError as e:
        click.echo(f"error: dispatch failed: {e}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    click.echo(f"Fix-up worker dispatched (assignment {new_id})")
    click.echo(f"  branch: {assignment.branch}")
    click.echo(f"  issue: #{assignment.issue_number}: {assignment.issue_title}")
    if test_output:
        click.echo(f"  test output included in briefing ({len(test_output)} chars)")


@click.command(
    "approve-plan",
    help=(
        "Approve a completed plan assignment and dispatch a work assignment "
        "to implement it."
    ),
)


@click.argument("assignment_id")
@_CONFIG_OPTION
def approve_plan(assignment_id: str, config_path: Path) -> None:
    from coord.board_service import read_board

    cfg = _load_config(config_path)
    board = read_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    if assignment.type != "plan":
        click.echo(
            f"error: assignment {assignment_id} is type {assignment.type!r}, not 'plan'. "
            "Only plan assignments can be approved with approve-plan.",
            err=True,
        )
        sys.exit(1)

    if assignment.status != "done":
        click.echo(
            f"error: assignment {assignment_id} is {assignment.status!r}, not 'done'. "
            "The plan worker must finish before you can approve it.",
            err=True,
        )
        sys.exit(1)

    plan_dict = _load_plan_for_assignment(assignment, assignment_id)
    if plan_dict is None:
        click.echo(
            f"error: no plan data found for assignment {assignment_id}.\n"
            "Possible reasons: the log is on a remote machine, or the worker "
            "did not output plan sections.\n"
            "Run 'coord notify' after the worker finishes to parse and cache the plan.",
            err=True,
        )
        sys.exit(1)

    plan_text = _plan_dict_to_text(plan_dict)

    # Build the enhanced briefing for the work assignment.
    original_briefing = (assignment.briefing or "").strip()
    separator = "\n\n" if original_briefing else ""
    enhanced_briefing = (
        original_briefing
        + separator
        + "Your plan was reviewed and approved. Implement exactly as described:\n\n"
        + plan_text
    ).strip()

    # Use files_modify from the plan as the allowed-files hint for the worker.
    from coord.plan_parser import WorkerPlan  # noqa: PLC0415
    plan_obj = WorkerPlan.from_dict(plan_dict)
    files_likely = plan_obj.files_modify or assignment.files_allowed or []

    click.echo(
        f"Approving plan {assignment_id}: "
        f"{assignment.repo_name} #{assignment.issue_number} — {assignment.issue_title}"
    )
    click.echo(f"  Dispatching work assignment to {assignment.machine_name}...")

    try:
        new_id = _dispatch_followup(
            cfg,
            assignment,
            enhanced_briefing,
            type="work",
            files_likely=files_likely,
            # The plan is read-only; its recorded branch is a throwaway
            # worktree name (and can be a stale/wrong capture).  Work must
            # branch fresh from the issue, not inherit the plan's branch.
            inherit_branch=False,
        )
    except httpx.HTTPError as e:
        click.echo(f"error: dispatch failed: {e}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    # Persist plan-stage SMOKE_TESTS onto the new work assignment so the
    # TUI surfaces them immediately — and so they survive even if the
    # work worker exits without re-emitting its own block.  The work
    # worker's later SMOKE_TESTS (captured by notify._capture_smoke_tests)
    # overrides this when present.
    if plan_obj.smoke_tests is not None:
        from coord.state import update_assignment_smoke_tests  # noqa: PLC0415
        update_assignment_smoke_tests(new_id, plan_obj.smoke_tests)

    click.echo(f"  Work assignment dispatched (assignment {new_id})")
    click.echo(f"  repo: {assignment.repo_name}  issue: #{assignment.issue_number}")
    click.echo(f"  Run: coord log {new_id} to follow progress")


@click.command(
    "reject-plan",
    help=(
        "Reject a completed plan assignment and re-dispatch for revision "
        "with additional guidance."
    ),
)


@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option(
    "--guidance",
    required=True,
    help="Guidance text explaining what to revise in the plan.",
)


def reject_plan(assignment_id: str, config_path: Path, guidance: str) -> None:
    from coord.board_service import read_board

    cfg = _load_config(config_path)
    board = read_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    if assignment.type != "plan":
        click.echo(
            f"error: assignment {assignment_id} is type {assignment.type!r}, not 'plan'. "
            "Only plan assignments can be rejected with reject-plan.",
            err=True,
        )
        sys.exit(1)

    if assignment.status != "done":
        click.echo(
            f"error: assignment {assignment_id} is {assignment.status!r}, not 'done'. "
            "The plan worker must finish before you can reject it.",
            err=True,
        )
        sys.exit(1)

    plan_dict = _load_plan_for_assignment(assignment, assignment_id)
    if plan_dict is None:
        click.echo(
            f"error: no plan data found for assignment {assignment_id}.\n"
            "Possible reasons: the log is on a remote machine, or the worker "
            "did not output plan sections.\n"
            "Run 'coord notify' after the worker finishes to parse and cache the plan.",
            err=True,
        )
        sys.exit(1)

    plan_text = _plan_dict_to_text(plan_dict)

    # Build the enhanced briefing for the revised plan assignment.
    original_briefing = (assignment.briefing or "").strip()
    separator = "\n\n" if original_briefing else ""
    enhanced_briefing = (
        original_briefing
        + separator
        + "Previous plan (rejected):\n\n"
        + plan_text
        + "\n\nGuidance:\n\n"
        + guidance.strip()
    ).strip()

    click.echo(
        f"Rejecting plan {assignment_id}: "
        f"{assignment.repo_name} #{assignment.issue_number} — {assignment.issue_title}"
    )
    click.echo(f"  Re-dispatching revised plan to {assignment.machine_name}...")

    try:
        new_id = _dispatch_followup(
            cfg,
            assignment,
            enhanced_briefing,
            type="plan",
            files_likely=list(assignment.files_allowed),
            # Revised plan is read-only too — don't inherit the prior
            # plan's throwaway branch.
            inherit_branch=False,
        )
    except httpx.HTTPError as e:
        click.echo(f"error: dispatch failed: {e}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    click.echo(f"  Revised plan assignment dispatched (assignment {new_id})")
    click.echo(f"  repo: {assignment.repo_name}  issue: #{assignment.issue_number}")
    click.echo(f"  Run: coord log {new_id} to follow progress")


@click.command(
    "resume-stuck",
    help="Stop a stuck worker and dispatch a continuation with guidance.",
)


@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option("--guidance", required=True, help="Guidance for the continuation worker.")
def resume_stuck(assignment_id: str, config_path: Path, guidance: str) -> None:
    from coord.board_service import read_board

    cfg = _load_config(config_path)
    board = read_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    if assignment.status != "running":
        click.echo(
            f"error: assignment {assignment_id} is {assignment.status!r}, "
            "can only resume-stuck a running assignment",
            err=True,
        )
        sys.exit(1)

    # Find the machine this assignment is running on
    machine = next(
        (m for m in cfg.machines if m.name == assignment.machine_name), None
    )
    if machine is None:
        click.echo(
            f"error: machine {assignment.machine_name!r} not in config", err=True
        )
        sys.exit(1)

    # Stop the current worker
    try:
        resp = httpx.post(
            f"http://{machine.host}:{AGENT_PORT}/cancel/{assignment_id}",
            timeout=10,
        )
        resp.raise_for_status()
        click.echo(f"Cancelled stuck worker on {machine.name}")
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        click.echo(
            f"warning: could not cancel worker on {machine.name}: {e} "
            "(may have already stopped)",
            err=True,
        )

    # Brief pause for cancellation to take effect
    time.sleep(2)

    # Retrieve the stuck message from the agent's progress data
    stuck_message = ""
    try:
        status_resp = httpx.get(
            f"http://{machine.host}:{AGENT_PORT}/status", timeout=5
        )
        if status_resp.status_code == 200:
            status_data = status_resp.json()
            # Check active and completed for progress info
            for entry in status_data.get("active", []) + status_data.get("completed", []):
                if entry.get("id") == assignment_id:
                    progress = entry.get("progress", {})
                    if progress and progress.get("stuck"):
                        stuck_message = progress["stuck"]
                    break
    except Exception:  # noqa: BLE001
        pass

    repo = cfg.repo(assignment.repo_name)
    if repo is None:
        click.echo(f"error: unknown repo {assignment.repo_name!r}", err=True)
        sys.exit(1)

    default_branch = repo.default_branch

    stuck_section = stuck_message if stuck_message else "(no stuck message captured)"

    briefing = (
        f"You are continuing work on issue #{assignment.issue_number}: {assignment.issue_title}\n\n"
        f"The previous worker got stuck on branch {assignment.branch or 'unknown'}. "
        f"You are already on that branch.\n"
        f"Do NOT start over — continue from where they left off.\n\n"
        f"## What was done\n"
        f"Run `git fetch origin && git log --oneline origin/{default_branch}..HEAD` to see previous work.\n"
        f"Run `git diff origin/{default_branch}...HEAD` to see the full diff.\n\n"
        f"## What the previous worker was stuck on\n"
        f"{stuck_section}\n\n"
        f"## Guidance\n"
        f"{guidance}\n\n"
        f"## Rules\n"
        f"- Continue from the existing branch, do not start over\n"
        f"- Commit your work and push with git push origin HEAD"
    )

    # Determine escalated model for the continuation worker.
    original_model = assignment.model or cfg.models.default
    escalated = cfg.models.next_model(original_model)
    if escalated != original_model:
        click.echo(f"  escalating model: {original_model} → {escalated}")

    try:
        new_id = _dispatch_followup(cfg, assignment, briefing, model=escalated)
    except httpx.HTTPError as e:
        click.echo(f"error: dispatch failed: {e}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    click.echo(f"Continuation dispatched (assignment {new_id})")
    click.echo(f"  branch: {assignment.branch or 'unknown'}")
    click.echo(f"  issue: #{assignment.issue_number}: {assignment.issue_title}")
    click.echo(f"  guidance: {guidance}")