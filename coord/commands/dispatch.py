"""Core dispatch commands: `assign`, `approve`, `plan`, `retry`, `stop`,
`inject`, `chat-continue`. The mode-specific `_dispatch_*` worker
implementations `assign` delegates to live in dispatch_workers.py.
Extracted from coord/cli.py (#747)."""

from __future__ import annotations

import dataclasses
import socket
import sys
from pathlib import Path

import click
import httpx

from coord import github_ops

from coord.commands._common import AGENT_PORT, _CONFIG_OPTION, _load_config
from coord.commands.dispatch_workers import (
    _dispatch_chat,
    _dispatch_fix_of,
    _dispatch_headless,
    _dispatch_interactive_work,
    _dispatch_merge_of,
    _dispatch_review_of,
    _dispatch_rework_of,
    _dispatch_smoke_of,
    _dispatch_troubleshoot,
)


@click.command(help="Brain proposes assignments for idle machines.")
@_CONFIG_OPTION
@click.option("--dry-run", is_flag=True, help="Plan without saving proposals.")
def plan(config_path: Path, dry_run: bool) -> None:
    from coord.brain import propose
    from coord.state import save_proposals, save_split_proposals

    cfg = _load_config(config_path)
    click.echo("Gathering context...", nl=False)
    sys.stdout.flush()

    from coord.brain import gather_context, build_prompt, call_claude, parse_proposals, parse_split_proposals, resolve_required_gates, SYSTEM_PROMPT
    context = gather_context(cfg)
    issue_count = sum(len(v) for v in context["issues_by_repo"].values())
    online = sum(1 for v in context["machine_status"].values() if v.get("status") != "offline" and "error" not in str(v))
    click.echo(f" {issue_count} issues across {len(cfg.repos)} repos, {online} machines online.")
    click.echo("Calling Claude (this may take 1-2 minutes)...", nl=False)
    sys.stdout.flush()

    try:
        prompt = build_prompt(cfg, context)
        response = call_claude(SYSTEM_PROMPT, prompt)
        proposals = parse_proposals(response)
        resolve_required_gates(proposals, cfg, context["issues_by_repo"])
        splits = parse_split_proposals(response)
    except RuntimeError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    if splits:
        click.echo(f"{len(splits)} split proposal(s):\n")
        for s in splits:
            click.echo(f"  [S{s.id}] {s.repo_name} #{s.issue_number}: {s.issue_title}")
            click.echo(f"      {s.rationale}")
            click.echo(f"      chunks ({len(s.chunks)}):")
            for j, chunk in enumerate(s.chunks, 1):
                click.echo(f"        {j}. {chunk.title}")
                click.echo(f"           {chunk.scope}")
            click.echo()

    if proposals:
        click.echo(f"{len(proposals)} assignment proposal(s):\n")
        for p in proposals:
            click.echo(f"  [{p.id}] {p.machine_name} → {p.repo_name} #{p.issue_number}: {p.issue_title}")
            click.echo(f"      {p.rationale}")
            if p.files_likely:
                click.echo(f"      files: {', '.join(p.files_likely)}")
            click.echo()

    if not proposals and not splits:
        click.echo("No assignments to propose.")
        return

    if dry_run:
        click.echo("(dry run — proposals not saved)")
    else:
        if proposals:
            save_proposals(proposals)
        if splits:
            save_split_proposals(splits)
        click.echo("Proposals saved.")
        if proposals:
            click.echo("Run `coord approve <ids>` to dispatch (e.g. coord approve 1,2)")
        if splits:
            click.echo("Run `coord split <ids>` to create sub-issues (e.g. coord split S1)")


@click.command(help="Dispatch approved assignments (comma-separated IDs).")
@click.argument("ids")
@_CONFIG_OPTION
@click.option("--dry-run", is_flag=True, help="Show what would be dispatched.")
@click.option(
    "--auto-pull",
    is_flag=True,
    help="Tell the agent to `git pull --ff-only` stale dependency repos before starting.",
)


@click.option(
    "--skip-freshness",
    is_flag=True,
    help="Skip the dependency freshness check (faster, no network for GH HEADs).",
)


def approve(
    ids: str, config_path: Path, dry_run: bool, auto_pull: bool, skip_freshness: bool
) -> None:
    from coord import freshness as fresh
    from coord.board_service import read_board, write_board
    from coord.deps import blocked_repos as compute_blocked, build_dep_graph, transitive_deps
    from coord.dispatch import compute_do_not_touch, dispatch, dispatch_with_retry, post_briefing
    from coord.network import classify_error, fetch_repos
    from coord.state import (
        clear_proposals,
        load_dispatched,
        load_proposals,
        record_dispatched,
    )

    cfg = _load_config(config_path)
    proposals = load_proposals()
    if not proposals:
        click.echo("No pending proposals. Run `coord plan` first.", err=True)
        sys.exit(1)

    try:
        selected_ids = [int(x.strip()) for x in ids.split(",")]
    except ValueError:
        click.echo("error: IDs must be comma-separated integers (e.g. 1,3)", err=True)
        sys.exit(2)

    selected = [p for p in proposals if p.id in selected_ids]
    missing = set(selected_ids) - {p.id for p in selected}
    if missing:
        click.echo(f"error: unknown proposal IDs: {missing}", err=True)
        sys.exit(2)

    # Warn about dependency-blocked repos
    board = read_board()
    blocked = compute_blocked(cfg.repos, board.active)
    for p in selected:
        if p.repo_name in blocked:
            click.echo(f"  warning: {p.repo_name} is blocked by upstream work:", err=True)
            for reason in blocked[p.repo_name]:
                click.echo(f"    - {reason}", err=True)

    in_flight = load_dispatched()

    # ── Claim pre-check ──────────────────────────────────────────────
    # Refuse any proposal whose issue is already being worked on (board
    # has an active assignment, or remote has an `issue-{N}-*` branch).
    from coord.claim import claim_message, find_work_claim

    unclaimed: list = []
    for p in selected:
        repo_cfg = cfg.repo(p.repo_name)
        if repo_cfg is None:
            unclaimed.append(p)
            continue
        claim = find_work_claim(
            p.issue_number, p.repo_name, repo_cfg.github, board
        )
        if claim is not None:
            click.echo(
                f"[{p.id}] skipping {p.repo_name} #{p.issue_number}: "
                f"{claim_message(claim)}",
                err=True,
            )
            continue
        unclaimed.append(p)

    if not unclaimed:
        click.echo("No proposals remain after claim check.", err=True)
        sys.exit(1)
    selected = unclaimed

    # ── Freshness pre-check ──────────────────────────────────────────
    machine_repos: dict[str, dict | None] = {}
    github_heads: dict[str, str | None] = {}
    if not skip_freshness and not dry_run:
        graph = build_dep_graph(cfg.repos)
        machines_needed = {p.machine_name for p in selected}
        for mname in machines_needed:
            machine = next((m for m in cfg.machines if m.name == mname), None)
            machine_repos[mname] = fetch_repos(machine) if machine else None

        repos_needed: set[str] = set()
        for p in selected:
            repos_needed.update(transitive_deps(p.repo_name, graph))
        for repo_name in repos_needed:
            repo_cfg = cfg.repo(repo_name)
            if repo_cfg is None:
                github_heads[repo_name] = None
                continue
            try:
                github_heads[repo_name] = github_ops.get_default_branch_head(
                    repo_cfg.github, repo_cfg.default_branch
                )
            except RuntimeError as e:
                click.echo(f"  warning: could not get HEAD of {repo_cfg.github}: {e}", err=True)
                github_heads[repo_name] = None

    # ── Auto-split advisory ───────────────────────────────────────────────
    if cfg.dispatch.auto_split:
        from coord.split_work import analyze_plan, format_chunks_summary

        for p in selected:
            chunks = analyze_plan(p.files_likely, cfg.dispatch)
            if len(chunks) > 1:
                click.echo(
                    f"  ⚠ [{p.id}] {p.repo_name} #{p.issue_number} touches "
                    f"{len(p.files_likely)} files (threshold: "
                    f"{cfg.dispatch.max_files_per_worker}) — consider splitting:"
                )
                click.echo(format_chunks_summary(chunks))

    for p in selected:
        click.echo(f"[{p.id}] {p.machine_name} → {p.repo_name} #{p.issue_number}: {p.issue_title}")
        # Resolve model so the dispatched record and board reflect what ran.
        if not p.model:
            p.model = cfg.models.default
        # Resolve required_gates: fall back to config default for proposals
        # that were saved before label-based gate resolution was wired in.
        if not p.required_gates:
            p.required_gates = list(cfg.pipeline.default_gates)
        if dry_run:
            click.echo("     (dry run — not dispatched)")
            continue

        pull_repos: list[str] = []
        if not skip_freshness:
            agent_repos = machine_repos.get(p.machine_name) or {}
            freshness = fresh.dependency_freshness(p, cfg, agent_repos, github_heads)
            needs = fresh.stale_or_dirty(freshness)
            if needs:
                for f in needs:
                    click.echo(
                        f"     dependency {f.repo_name}: {f.state}"
                        + (f" ({f.error})" if f.error else ""),
                        err=True,
                    )
                if auto_pull:
                    pull_repos = [f.repo_name for f in needs if f.state == fresh.STALE]
                    if pull_repos:
                        click.echo(f"     will pull on agent before worker: {pull_repos}")
                else:
                    addendum = fresh.format_briefing_addendum(freshness)
                    if addendum:
                        p.briefing = (p.briefing or "") + addendum

        def _on_retry(attempt, max_r, state, reason, wait):
            click.echo(
                f"     retry {attempt}/{max_r} after {state} ({reason}), "
                f"waiting {wait:.0f}s...",
                err=True,
            )

        try:
            response = dispatch_with_retry(
                p, cfg,
                max_retries=cfg.concurrency.max_retries,
                backoff_base=cfg.concurrency.backoff_base,
                pull_repos=pull_repos,
                on_retry=_on_retry,
            )
        except httpx.HTTPError as e:
            state, reason = classify_error(e)
            click.echo(
                f"     dispatch failed after {cfg.concurrency.max_retries} retries: "
                f"{p.machine_name} {state} — {reason}",
                err=True,
            )
            continue
        except ValueError as e:
            click.echo(f"     dispatch failed: {e}", err=True)
            continue
        assignment_id = response.get("id", "pending")
        click.echo(f"     dispatched to agent server (assignment {assignment_id})")

        repo = cfg.repo(p.repo_name)
        if repo is not None:
            record_dispatched(
                assignment_id=assignment_id,
                proposal=p,
                repo_github=repo.github,
                provider_name=response.get("_provider_name"),
            )

        try:
            do_not_touch = compute_do_not_touch(p, peers=selected, in_flight=in_flight)
            post_briefing(p, cfg, assignment_id=assignment_id, do_not_touch=do_not_touch)
            click.echo("     briefing posted to GitHub")
        except Exception as e:
            click.echo(f"     briefing post failed: {e}", err=True)

        if not dry_run and p is not selected[-1] and cfg.concurrency.stagger_seconds > 0:
            import time as _time
            click.echo(f"     staggering {cfg.concurrency.stagger_seconds:.0f}s before next dispatch...")
            _time.sleep(cfg.concurrency.stagger_seconds)

    if not dry_run:
        clear_proposals()
        board = read_board()
        board.round_number += 1
        write_board(board)
        click.echo("\nPending proposals cleared. Board saved.")

        # Mark session start on first dispatch of the session
        from coord.state import load_session, write_session_start
        session = load_session()
        if session is None or session.get("clean_shutdown", True):
            write_session_start()


@click.command(help="Directly assign an issue to a machine, bypassing coord plan.")
@click.argument("machine")
@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
@click.option("--briefing", default="", help="Optional briefing text for the worker.")
@click.option(
    "--briefing-file",
    "briefing_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help=(
        "#569: read the briefing from a file instead of --briefing. Avoids "
        "shell-quoting a multi-line briefing on the command line (a multi-line "
        "--briefing typed into a PTY shell strands it at `quote>`). Overrides "
        "--briefing when both are given."
    ),
)


@click.option(
    "--model",
    default=None,
    help="Claude model tier (haiku, sonnet, opus). Defaults to models.default.",
)


@click.option("--dry-run", is_flag=True, help="Show what would be dispatched.")
@click.option(
    "--plan-only",
    is_flag=True,
    help=(
        "Dispatch a read-only planning worker. The worker reads the codebase "
        "and outputs a structured plan (FILES_READ, FILES_MODIFY, APPROACH, "
        "RISKS, ESTIMATE) without writing code or modifying files. "
        "No worktree or feature branch is created."
    ),
)


@click.option(
    "--no-plan",
    is_flag=True,
    help=(
        "Force a direct work dispatch even when dispatch.require_plan is true "
        "in coordinator.yml. Has no effect when require_plan is false."
    ),
)


@click.option(
    "--force",
    is_flag=True,
    help="Bypass claim detection only (use when retrying after infra failures).",
)


@click.option(
    "--no-pull",
    is_flag=True,
    help=(
        "Skip the auto-pull of stale dependency repos on the agent. "
        "The briefing still carries a 'pull these before building' "
        "addendum so the worker is aware (#267)."
    ),
)


@click.option(
    "--skip-freshness",
    is_flag=True,
    help=(
        "Skip the dependency freshness check entirely — faster, no "
        "network for GH HEADs.  Matches `coord approve --skip-freshness` (#267)."
    ),
)


@click.option(
    "--interactive",
    is_flag=True,
    help=(
        "HUMAN-ATTENDED launcher (#437): start interactive `claude` "
        "locally on THIS terminal with the briefing PRE-FILLED in the "
        "input box.  You press Enter to submit and Ctrl-C / `/exit` to "
        "end the session.  Used for the subscription-billed path; the "
        "coordinator does NOT watch the TTY, does NOT auto-submit, does "
        "NOT advance the pipeline from session output.  This bypasses "
        "the agent HTTP server and runs `claude` as a child of your "
        "shell."
    ),
)


@click.option(
    "--review-of",
    "review_of",
    default=None,
    help=(
        "Launch a human-attended interactive REVIEW of completed work "
        "assignment <ID> (the work id from `coord status`). Implies a "
        "review-shaped dispatch: type=review linked to the work (so the merge "
        "gate's has_approved_review can find the verdict), the diff-only "
        "review briefing, and NO isolated worktree (read-only in the live "
        "checkout). Report your verdict with `coord report-result --verdict "
        "approve|request-changes`. Requires --interactive; local-only for now "
        "(remote review is Track B / #486)."
    ),
)


@click.option(
    "--fix-of",
    "fix_of",
    default=None,
    help=(
        "Leg 3 (#517): launch a human-attended interactive FIX for a review "
        "assignment <ID> whose verdict was request-changes. Continues on the "
        "reviewed work's EXISTING branch (so the same PR is updated, not a new "
        "orphan branch), is briefed with the reviewer's findings, and bumps "
        "review_iteration so the next review can scope to just the fix delta. "
        "ALSO accepts a WORK assignment id whose test gate FAILED (#581): the "
        "fix is then briefed with the recorded test-failure story. "
        "Requires --interactive; local-only for now (remote is Track B / #486)."
    ),
)


@click.option(
    "--troubleshoot",
    "troubleshoot",
    is_flag=True,
    default=False,
    help=(
        "#569: launch a human-attended READ-ONLY diagnostic session for a "
        "stalled item. Runs in the LIVE checkout with NO claim and NO worktree "
        "(so it never conflicts with the item's own in-progress claim), "
        "type=troubleshoot, briefed from --briefing/--briefing-file. Requires "
        "--interactive; local-only."
    ),
)


@click.option(
    "--chat",
    "chat",
    is_flag=True,
    default=False,
    help=(
        "#628: launch a human-attended 'Chat about issue' session — a live "
        "interactive `claude` seeded with everything we know about the issue "
        "(body, comments, board state). Ask open questions ('is this still "
        "needed?', 'what milestone?', 'sketch the UX'), diagnose a stall, edit "
        "the issue (via `coord issue edit`), and send it to Pending (`coord "
        "ready`). Runs in the LIVE checkout with NO claim and NO worktree; "
        "type=chat. Mutates the ISSUE through coord (never raw gh) and never the "
        "code/checkout. Requires --interactive; local-only."
    ),
)


@click.option(
    "--rework-of",
    "rework_of",
    default=None,
    help=(
        "#563: launch a human-attended interactive REWORK of an existing branch. "
        "Accepts a work assignment ID (resolves its branch) or a branch name "
        "directly. Continues on the EXISTING branch (no orphan branch), seeds "
        "the session with the operator-supplied --briefing verbatim, and bumps "
        "review_iteration so the reworked branch is re-reviewed before merge. "
        "Requires --interactive and --briefing; works local and remote (same "
        "worktree + push-back as --fix-of)."
    ),
)


@click.option(
    "--smoke-of",
    "smoke_of",
    default=None,
    help=(
        "Leg 3c / A3 (#517, #350, #581): launch a human-attended interactive "
        "TESTING agent for completed work assignment <ID>. The agent lists the "
        "smoke tests, pulls the build artifact, guides you through running it, "
        "interviews you about what you saw, and records the verdict with "
        "`coord test --passed|--fail`. Read-only tools, NO worktree (runs in the "
        "live checkout). Requires --interactive; local-only for now."
    ),
)


@click.option(
    "--merge-of",
    "merge_of",
    default=None,
    help=(
        "Leg 3c (#517, #306): launch a human-attended interactive MERGE agent "
        "for completed+approved work assignment <ID>. Continues the work branch "
        "in a worktree, fetches + rebases it onto the repo's default branch "
        "(proactive rebase, #306), resolves mechanical conflicts, runs the "
        "tests, pushes --force-with-lease, then guides you to merge. Requires "
        "--interactive; local-only for now."
    ),
)


def assign(
    machine: str,
    repo: str,
    issue: int,
    config_path: Path,
    briefing: str,
    model: str | None,
    dry_run: bool,
    plan_only: bool,
    no_plan: bool,
    force: bool,
    no_pull: bool,
    skip_freshness: bool,
    interactive: bool,
    review_of: str | None,
    fix_of: str | None,
    briefing_file: str | None,
    troubleshoot: bool,
    chat: bool,
    rework_of: str | None,
    smoke_of: str | None,
    merge_of: str | None,
) -> None:
    cfg = _load_config(config_path)

    # Validate machine exists in config
    machine_obj = next((m for m in cfg.machines if m.name == machine), None)
    if machine_obj is None:
        click.echo(
            f"error: machine {machine!r} not in coordinator.yml "
            f"(have: {[m.name for m in cfg.machines]})",
            err=True,
        )
        sys.exit(2)

    # Validate repo exists in config
    repo_cfg = cfg.repo(repo)
    if repo_cfg is None:
        click.echo(
            f"error: repo {repo!r} not in coordinator.yml "
            f"(have: {[r.name for r in cfg.repos]})",
            err=True,
        )
        sys.exit(2)

    # Validate machine can work on this repo
    if not machine_obj.can_work_on(repo):
        click.echo(
            f"error: machine {machine!r} does not list repo {repo!r} "
            f"(has: {machine_obj.repos})",
            err=True,
        )
        sys.exit(2)

    # Refuse direct assignment to a paused machine — `coord pause` exists
    # so the user can explicitly steer work away.  If they meant to dispatch
    # anyway they should `coord unpause` first.
    from coord.machine_pause import is_paused as _is_paused
    if _is_paused(machine):
        click.echo(
            f"error: machine {machine!r} is paused; run `coord unpause {machine}` first",
            err=True,
        )
        sys.exit(2)

    # Fetch the issue title from GitHub
    try:
        issue_data = github_ops.get_issue(repo_cfg.github, issue)
    except RuntimeError as e:
        click.echo(f"error: could not fetch issue #{issue}: {e}", err=True)
        sys.exit(1)
    issue_title = issue_data.get("title", f"Issue #{issue}")

    # --briefing-file (#569): read the briefing from a file; this avoids having
    # to shell-quote a multi-line briefing on the command line (a multi-line
    # --briefing typed into a PTY shell strands it at `quote>`).  Overrides
    # --briefing when both are given.
    if briefing_file:
        briefing = Path(briefing_file).read_text(encoding="utf-8")

    # Auto-generate briefing from issue body when none provided.
    if not briefing:
        issue_body = issue_data.get("body", "")
        if issue_body:
            briefing = f"Issue #{issue}: {issue_title}\n\n{issue_body}"

    # A1 (interactive-mode migration): --review-of is a flavour of the
    # human-attended interactive launcher, so it requires --interactive.
    if review_of is not None and not interactive:
        click.echo("error: --review-of requires --interactive", err=True)
        sys.exit(2)

    # Leg 3 (#517): --fix-of is a sibling flavour — a human-attended fix of a
    # request-changes review.  Same interactive requirement; mutually exclusive
    # with --review-of (a dispatch is one shape or the other).
    if fix_of is not None and not interactive:
        click.echo("error: --fix-of requires --interactive", err=True)
        sys.exit(2)
    if fix_of is not None and review_of is not None:
        click.echo("error: --fix-of and --review-of are mutually exclusive", err=True)
        sys.exit(2)

    # #569: --troubleshoot is a read-only diagnostic flavour — requires
    # --interactive.
    if troubleshoot and not interactive:
        click.echo("error: --troubleshoot requires --interactive", err=True)
        sys.exit(2)

    # #628: --chat (Chat about issue) — human-attended, requires --interactive.
    if chat and not interactive:
        click.echo("error: --chat requires --interactive", err=True)
        sys.exit(2)

    # #563: --rework-of — requires --interactive, and --briefing so the operator
    # always supplies explicit rework instructions.
    if rework_of is not None and not interactive:
        click.echo("error: --rework-of requires --interactive", err=True)
        sys.exit(2)
    if rework_of is not None and not (briefing or "").strip():
        click.echo(
            "error: --rework-of requires --briefing (supply the rework instructions).",
            err=True,
        )
        sys.exit(2)

    # Leg 3c (#517): --smoke-of (interactive testing agent) and --merge-of
    # (interactive merge agent) — each requires --interactive.
    if smoke_of is not None and not interactive:
        click.echo("error: --smoke-of requires --interactive", err=True)
        sys.exit(2)
    if merge_of is not None and not interactive:
        click.echo("error: --merge-of requires --interactive", err=True)
        sys.exit(2)

    # All interactive flavours are mutually exclusive — a dispatch is exactly
    # one shape (review / fix / troubleshoot / rework / smoke / merge).
    _interactive_flavours = [
        ("--review-of", review_of is not None),
        ("--fix-of", fix_of is not None),
        ("--troubleshoot", troubleshoot),
        ("--chat", chat),
        ("--rework-of", rework_of is not None),
        ("--smoke-of", smoke_of is not None),
        ("--merge-of", merge_of is not None),
    ]
    _set_flavours = [name for name, on in _interactive_flavours if on]
    if len(_set_flavours) > 1:
        click.echo(
            f"error: {', '.join(_set_flavours)} are mutually exclusive "
            "(a dispatch is exactly one shape).",
            err=True,
        )
        sys.exit(2)

    # #437: HUMAN-ATTENDED branch.  When --interactive is set, we run
    # interactive `claude` as a child of THIS shell with the briefing
    # PRE-FILLED in the input box.  No HTTP agent, no Proposal, no
    # GitHub posting, no board update — the operator drives the session
    # and closes it manually.  This is the subscription-billed escape
    # hatch from Anthropic ToS §3.7 metering.  Resolving
    # ClaudePtyProvider here AND asserting its capabilities are flagged
    # human_attended_only is the structural guarantee that this path is
    # the only one that can launch it; the unattended dispatch sites
    # (dispatch/review/reconcile) refuse the same capability.
    #
    # assign() is a thin dispatcher (#746): the validation above is the
    # only logic that lives here.  Every dispatch SHAPE (review / fix /
    # troubleshoot / chat / rework / smoke / merge / plain-interactive /
    # headless) is a self-contained _dispatch_* function below — each one
    # mirrors exactly what used to be a top-level if/elif branch in this
    # function, taking the already-validated values as parameters.
    if interactive:
        setup = _build_interactive_launch_setup(
            machine=machine, repo=repo, issue=issue, machine_obj=machine_obj,
        )
        provider = setup.provider
        _is_local = setup.is_local
        _svc = setup.svc
        _interactive_board = setup.interactive_board
        _issue_ctx = setup.issue_ctx
        _ctx_write_hint = setup.ctx_write_hint

        if review_of is not None:
            _dispatch_review_of(
                machine=machine, repo=repo, issue=issue, briefing=briefing,
                model=model, dry_run=dry_run, review_of=review_of,
                cfg=cfg, machine_obj=machine_obj, repo_cfg=repo_cfg,
                issue_data=issue_data, issue_title=issue_title,
                provider=provider, _is_local=_is_local, _svc=_svc,
                _interactive_board=_interactive_board, _issue_ctx=_issue_ctx,
            )
            return
        if smoke_of is not None:
            _dispatch_smoke_of(
                machine=machine, repo=repo, issue=issue, briefing=briefing,
                model=model, dry_run=dry_run, smoke_of=smoke_of,
                cfg=cfg, machine_obj=machine_obj, repo_cfg=repo_cfg,
                issue_data=issue_data, issue_title=issue_title,
                provider=provider, _is_local=_is_local, _svc=_svc,
                _interactive_board=_interactive_board, _issue_ctx=_issue_ctx,
            )
            return
        if troubleshoot:
            _dispatch_troubleshoot(
                machine=machine, repo=repo, issue=issue, briefing=briefing,
                briefing_file=briefing_file, model=model, dry_run=dry_run,
                cfg=cfg, machine_obj=machine_obj, repo_cfg=repo_cfg,
                issue_title=issue_title, provider=provider, _is_local=_is_local,
                _issue_ctx=_issue_ctx, _ctx_write_hint=_ctx_write_hint,
            )
            return
        if chat:
            _dispatch_chat(
                machine=machine, repo=repo, issue=issue, briefing=briefing,
                briefing_file=briefing_file, model=model, dry_run=dry_run,
                cfg=cfg, machine_obj=machine_obj, repo_cfg=repo_cfg,
                issue_title=issue_title, provider=provider, _is_local=_is_local,
                _issue_ctx=_issue_ctx, _ctx_write_hint=_ctx_write_hint,
            )
            return
        if fix_of is not None:
            _dispatch_fix_of(
                machine=machine, repo=repo, issue=issue, briefing=briefing,
                model=model, dry_run=dry_run, force=force, fix_of=fix_of,
                cfg=cfg, machine_obj=machine_obj, repo_cfg=repo_cfg,
                issue_title=issue_title, provider=provider, _is_local=_is_local,
                _svc=_svc, _interactive_board=_interactive_board,
                _issue_ctx=_issue_ctx, _ctx_write_hint=_ctx_write_hint,
            )
            return
        if rework_of is not None:
            _dispatch_rework_of(
                machine=machine, repo=repo, issue=issue, briefing=briefing,
                model=model, dry_run=dry_run, force=force, rework_of=rework_of,
                cfg=cfg, machine_obj=machine_obj, repo_cfg=repo_cfg,
                issue_title=issue_title, provider=provider, _is_local=_is_local,
                _svc=_svc, _interactive_board=_interactive_board, _issue_ctx=_issue_ctx,
            )
            return
        if merge_of is not None:
            _dispatch_merge_of(
                machine=machine, repo=repo, issue=issue, briefing=briefing,
                model=model, dry_run=dry_run, force=force, merge_of=merge_of,
                cfg=cfg, machine_obj=machine_obj, repo_cfg=repo_cfg,
                issue_title=issue_title, provider=provider, _is_local=_is_local,
                _svc=_svc, _interactive_board=_interactive_board, _issue_ctx=_issue_ctx,
            )
            return

        _dispatch_interactive_work(
            machine=machine, repo=repo, issue=issue, briefing=briefing,
            model=model, dry_run=dry_run, plan_only=plan_only, no_plan=no_plan,
            force=force, cfg=cfg, machine_obj=machine_obj, repo_cfg=repo_cfg,
            issue_title=issue_title, provider=provider, _is_local=_is_local,
            _issue_ctx=_issue_ctx, _ctx_write_hint=_ctx_write_hint,
        )
        return

    _dispatch_headless(
        machine=machine, repo=repo, issue=issue, briefing=briefing,
        model=model, dry_run=dry_run, plan_only=plan_only, no_plan=no_plan,
        force=force, no_pull=no_pull, skip_freshness=skip_freshness,
        cfg=cfg, machine_obj=machine_obj, repo_cfg=repo_cfg,
        issue_data=issue_data, issue_title=issue_title,
    )


@dataclasses.dataclass
class _InteractiveLaunchSetup:
    """Shared one-time setup for every `coord assign --interactive` flavour
    (review/fix/troubleshoot/chat/rework/smoke/merge/plain).  Built once per
    dispatch by :func:`_build_interactive_launch_setup` and threaded into
    whichever ``_dispatch_*`` function the flavour flags select.
    """

    provider: object
    is_local: bool
    svc: object
    interactive_board: object
    issue_ctx: str
    ctx_write_hint: str


def _build_interactive_launch_setup(
    *,
    machine: str,
    repo: str,
    issue: int,
    machine_obj: object,
) -> _InteractiveLaunchSetup:
    # #466: The interactive launcher path now CLAIMS the issue and
    # RECORDS the dispatched assignment up front (it used to write
    # nothing then sys.exit), and on session exit invokes the
    # git-floor backstop in :func:`finalize_interactive_exit` so the
    # board ALWAYS gets a terminal completion — even if the human
    # closed the TTY without typing `coord report-result`.  Both the
    # backstop and the report-result subcommand write through the
    # single :mod:`coord.issue_store` seam so the future #183
    # IssueStore + coordination MCP can slot in without changing any
    # of these call sites.

    from coord.providers import ClaudePtyProvider  # noqa: PLC0415

    provider = ClaudePtyProvider()
    caps = provider.capabilities()
    # Structural guard: confirm we wired the right backend.
    # Use RuntimeError (not assert) so this is never silently removed
    # when Python runs with -O.
    if not caps.human_attended_only:
        raise RuntimeError(
            "BUG: --interactive resolved a provider whose capabilities do "
            "NOT report human_attended_only=True; refusing to launch."
        )

    # Detect whether the target machine is the local machine so we can
    # choose the local TTY path vs the remote SSH+tmux path (#494).
    # Mirrors the hostname-matching logic in _save_config_snapshot.
    _local_hn = socket.gethostname().split(".")[0].lower()
    _is_local = (
        machine_obj.name.lower() == _local_hn
        or machine_obj.host.split(".")[0].lower() == _local_hn
    )

    # #590/#749: on a thin client the local board/DB is empty, so resolve the
    # interactive-launch target (--review-of/--fix-of/--rework-of/--smoke-of/
    # --merge-of) from the daemon's board, and skip the local post-dispatch
    # save_board (record_dispatched_assignment already routed the row to the
    # daemon; a local save would write/resurrect an empty local coord.db).
    from coord.board_service import read_board as _read_interactive_board
    from coord.board_service import resolve as _resolve_svc  # noqa: PLC0415

    _svc = _resolve_svc()

    def _interactive_board(_local_build):
        """The board used to resolve a launch target: routes through
        board_service.read_board() (daemon when configured, else local) —
        *_local_build* (each call site's own ``build_board``) is accepted for
        backward-compat call-site signatures but no longer called directly."""
        del _local_build
        return _read_interactive_board()

    # #603: the per-issue context digest, prepended to the TOP of EVERY
    # interactive briefing below so each agent reads prior-attempt findings
    # (cross-repo deps, approaches already tried, hard constraints) first.
    # Computed once per dispatch; "" when there's no context (no-op prefix).
    from coord.state import issue_context_block as _issue_context_block  # noqa: PLC0415

    _issue_ctx = _issue_context_block(repo, issue)

    # #603 write-path hint: interactive agents run in the operator's
    # environment (so `coord` is on PATH, unlike #402-PATH-stripped -p
    # workers).  Tell the implementer flavours to record durable findings so
    # the next agent doesn't rediscover them.
    _ctx_write_hint = (
        "\n\n## Record durable findings for future agents (#603)\n"
        "If you discover something a LATER agent on this issue must know — a "
        "cross-repo dependency (another repo's branch/commit you had to "
        "pull), an approach that FAILED and why, or a non-obvious constraint "
        "— record it so it survives to the next attempt:\n"
        f'  `coord context add {repo} {issue} "<one-line finding>" --pin`  '
        "(--pin for a hard dependency/constraint; omit for a normal note).\n"
        "It is injected at the TOP of every later briefing for this issue — "
        "don't rely on memory or the PR alone.\n"
    )
    return _InteractiveLaunchSetup(
        provider=provider,
        is_local=_is_local,
        svc=_svc,
        interactive_board=_interactive_board,
        issue_ctx=_issue_ctx,
        ctx_write_hint=_ctx_write_hint,
    )


@click.command(help="Send a user message to a running worker mid-session.")
@click.argument("assignment_id")
@click.argument("text", nargs=-1, required=True)
@_CONFIG_OPTION
def inject(assignment_id: str, text: tuple[str, ...], config_path: Path) -> None:
    """Inject TEXT as a new user message into the running worker's session.

    The worker picks the message up at its next turn boundary — between
    tool calls, not mid-tool.  Useful for adding guidance to a worker
    that's going off the rails without having to stop + re-dispatch.
    """
    from coord.board_service import read_board
    from coord.network import inject_message

    cfg = _load_config(config_path)
    board = read_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    machine = next(
        (m for m in cfg.machines if m.name == assignment.machine_name), None
    )
    if machine is None:
        click.echo(f"error: machine {assignment.machine_name!r} not in config", err=True)
        sys.exit(1)

    message = " ".join(text).strip()
    if not message:
        click.echo("error: message text is empty", err=True)
        sys.exit(2)

    try:
        status, body = inject_message(machine, assignment_id, message)
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        click.echo(f"error: could not reach agent on {machine.name}: {e}", err=True)
        sys.exit(1)

    if status == 202:
        click.echo(
            f"Message delivered to {assignment.repo_name} #{assignment.issue_number} "
            f"on {machine.name}"
        )
    else:
        click.echo(
            f"error: agent rejected message (HTTP {status}): {body.get('error', body)}",
            err=True,
        )
        sys.exit(1)


@click.command(name="chat-continue", help="Continue a finished chat session with a new message.")
@click.argument("prior_assignment_id")
@click.argument("text", nargs=-1, required=True)
@_CONFIG_OPTION
def chat_continue(
    prior_assignment_id: str,
    text: tuple[str, ...],
    config_path: Path,
) -> None:
    """Re-dispatch a finished refinement assignment with TEXT as the next user turn.

    Looks up the claude session ID from the prior assignment and passes
    ``--resume <session_id>`` to the next worker so it loads the full
    conversation history before seeing TEXT as the next user message.

    Prints the new assignment ID on stdout so the TUI can bind to it.
    Does NOT post a GitHub briefing comment (chat turns are developer-side
    conversation, not issue activity).
    """
    from coord.db import get_connection
    from coord.dispatch import dispatch
    from coord.models import Proposal
    from coord.state import record_dispatched

    cfg = _load_config(config_path)

    conn = get_connection()
    row = conn.execute(
        "SELECT assignment_id, machine_name, repo_name, issue_number, issue_title, "
        "claude_session_id, type FROM assignments WHERE assignment_id=?",
        (prior_assignment_id,),
    ).fetchone()
    if row is None:
        click.echo(
            f"error: assignment {prior_assignment_id!r} not found in DB", err=True
        )
        sys.exit(1)

    # column may not exist on very old DBs that haven't migrated yet
    try:
        claude_session_id = row["claude_session_id"]
    except (IndexError, KeyError):
        claude_session_id = None

    machine_name = row["machine_name"]
    repo_name = row["repo_name"]
    issue_number = row["issue_number"]
    issue_title = row["issue_title"]
    message_text = " ".join(text).strip()

    # #316: preserve the chat type so the agent server uses the right system
    # prompt and tool restrictions on continuation.  The known chat types are
    # "refinement", "test-chat", and "new-issue-chat"; anything else falls
    # back to "refinement" (the original behaviour before type-preservation).
    _CHAT_TYPES = {"refinement", "test-chat", "new-issue-chat"}
    try:
        prior_type: str = row["type"] or "refinement"
    except (IndexError, KeyError):
        prior_type = "refinement"
    if prior_type not in _CHAT_TYPES:
        prior_type = "refinement"

    # #315: if the DB doesn't have the session_id yet, fetch it directly
    # from the agent's /status endpoint.  The notify cycle (typically every
    # 30s) is what syncs session_id from agent → DB; if the user types a
    # second chat message before notify catches up, the DB row is still
    # NULL even though the agent captured the session_id in memory.
    # Without this fallback every fast follow-up submit fails with
    # "no session ID captured" and the TUI's bind waits 30s and times out.
    if not claude_session_id:
        from coord.network import fetch_status  # noqa: PLC0415
        machine_for_status = next(
            (m for m in cfg.machines if m.name == machine_name), None,
        )
        if machine_for_status is not None:
            status_result = fetch_status(machine_for_status)
            if status_result.ok and status_result.data:
                # /status returns {"active": [...], "completed": [...]}
                # each entry is AgentAssignment.to_dict() with an `id` field
                for bucket in ("active", "completed"):
                    for entry in status_result.data.get(bucket, []):
                        if entry.get("id") == prior_assignment_id:
                            sid = entry.get("claude_session_id")
                            if isinstance(sid, str) and sid:
                                claude_session_id = sid
                                # Persist to DB so subsequent calls (and the
                                # coordinator's notify loop) don't re-fetch.
                                try:
                                    from coord.state import update_assignment_claude_session_id  # noqa: PLC0415
                                    update_assignment_claude_session_id(
                                        prior_assignment_id, sid,
                                    )
                                except Exception:  # noqa: BLE001
                                    pass
                            break
                    if claude_session_id:
                        break

    if not claude_session_id:
        click.echo(
            f"error: assignment {prior_assignment_id!r} has no session ID captured — "
            "agent has no session_id for this assignment (worker may not have "
            "emitted system.init, or the agent has restarted and forgotten it)",
            err=True,
        )
        sys.exit(1)

    repo_cfg = cfg.repo(repo_name)
    if repo_cfg is None:
        click.echo(f"error: repo {repo_name!r} not found in config", err=True)
        sys.exit(1)

    # Verify the target machine exists; warn but don't abort if missing
    # (the agent might still be reachable by name even if not in this config).
    machine = next((m for m in cfg.machines if m.name == machine_name), None)
    if machine is None:
        click.echo(
            f"warning: machine {machine_name!r} not in config — dispatch may fail",
            err=True,
        )

    # #315/#314/#316: use the type from the prior assignment so the agent
    # server uses the right system prompt and tool restrictions on continuation.
    # resume_session_id passes --resume so the full prior conversation is
    # loaded before the new user message is appended.
    proposal = Proposal(
        id=0,  # not inserted into proposals table; dummy value
        machine_name=machine_name,
        repo_name=repo_name,
        issue_number=issue_number,
        issue_title=issue_title,
        rationale="chat continuation",
        briefing=message_text,
        type=prior_type,
        resume_session_id=claude_session_id,
    )

    try:
        response = dispatch(proposal, cfg)
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: dispatch failed: {e}", err=True)
        sys.exit(1)

    assignment_id = response.get("id", "pending")

    # Record in coordinator DB so the board / TUI / notify see it.
    record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github=repo_cfg.github,
        provider_name=response.get("_provider_name"),
    )

    # Print the new assignment ID on stdout so callers (e.g. TUI) can bind.
    click.echo(assignment_id)


@click.command(help="Cancel a running assignment.")
@click.argument("assignment_id")
@_CONFIG_OPTION
def stop(assignment_id: str, config_path: Path) -> None:
    from coord.board_service import read_board, write_board

    cfg = _load_config(config_path)
    board = read_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    machine = next(
        (m for m in cfg.machines if m.name == assignment.machine_name), None
    )
    if machine is None:
        click.echo(f"error: machine {assignment.machine_name!r} not in config", err=True)
        sys.exit(1)

    try:
        resp = httpx.post(
            f"http://{machine.host}:{AGENT_PORT}/cancel/{assignment_id}",
            timeout=10,
        )
        resp.raise_for_status()
        click.echo(f"Assignment {assignment_id} cancelled on {machine.name}")
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        click.echo(f"warning: could not reach agent on {machine.name}: {e}", err=True)

    board.mark_failed_by_id(assignment_id)
    write_board(board)
    click.echo(f"Board updated: {assignment.repo_name} #{assignment.issue_number} marked failed")


@click.command(help="Re-dispatch a failed assignment to a different machine.")
@click.argument("assignment_id")
@_CONFIG_OPTION
def retry(assignment_id: str, config_path: Path) -> None:
    from coord.board_service import read_board, write_board
    from coord.reconcile import _reassign

    cfg = _load_config(config_path)
    board = read_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)
    if assignment.status != "failed":
        click.echo(
            f"error: assignment {assignment_id} is {assignment.status!r}, not failed. "
            f"Only failed assignments can be retried.",
            err=True,
        )
        sys.exit(1)

    # Determine escalated model for the retry.
    original_model = assignment.model or cfg.models.default
    escalated = cfg.models.next_model(original_model)
    if escalated != original_model:
        click.echo(f"  escalating model: {original_model} → {escalated}")

    result = _reassign(assignment, board, cfg, model=escalated)
    if result is None:
        click.echo("error: no available machine to retry on", err=True)
        sys.exit(1)

    write_board(board)
    click.echo(
        f"Retried: {result.machine_name} → {result.repo_name} "
        f"#{result.issue_number} (assignment {result.assignment_id})"
    )