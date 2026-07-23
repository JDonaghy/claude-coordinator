"""`coord merge` and the rest of the merge-queue surface: `verify-merge`,
`reconcile-merges`, `bounce`, `post-pending-reviews`. Extracted from
coord/cli.py (#747)."""

from __future__ import annotations

import sys
from pathlib import Path

import click


from coord.commands._common import _CONFIG_OPTION, _load_config
from coord.models import WORK_LIKE_TYPES


def _machine_for_assignment(board, assignment_id: str | None) -> str | None:
    """Return the machine name that ran *assignment_id*, or None.

    Used by ``coord merge`` (#241) to prefer dispatching a conflict-fix to
    the original worker's machine — that machine already has the repo
    checked out, the branch present, and the test deps installed.
    """
    if assignment_id is None or board is None:
        return None
    target = board.find_by_id(assignment_id)
    return target.machine_name if target is not None else None


@click.command(
    "verify-merge",
    help=(
        "Self-check a --merge-of rebase before reporting done (#604). Run from "
        "inside the merge worktree: `coord verify-merge <work_aid>`. Reports how "
        "many commits the branch is still MISSING from the default branch "
        "(`default-ahead`, must be 0), the commits it adds, and any FOREIGN "
        "commits (referencing a different issue) — the signature of a botched "
        "rebase that dragged in unrelated history. Exits non-zero when the "
        "branch is not merge-ready."
    ),
)


@click.argument("work_aid")
@click.option(
    "--path",
    "path_opt",
    type=click.Path(file_okay=False),
    default=None,
    help="Worktree to check (default: current directory).",
)


@click.option(
    "--repo",
    "repo_opt",
    default=None,
    help=(
        "Repo name — fallback when the assignment is not found on the board "
        "(thin-client machines where the board lives on the daemon, #681)."
    ),
)


@click.option(
    "--issue-number",
    "issue_number_opt",
    type=int,
    default=None,
    help=(
        "Issue number — fallback when the assignment is not found on the board "
        "(thin-client machines where the board lives on the daemon, #681)."
    ),
)


@_CONFIG_OPTION
def verify_merge(
    work_aid: str,
    path_opt: str | None,
    repo_opt: str | None,
    issue_number_opt: int | None,
    config_path: Path,
) -> None:
    """``coord verify-merge <work_aid>`` — git-truth check of a merge-prep branch.

    Resolves the issue + default branch from the *work* assignment id (the same
    id passed to ``coord assign --merge-of``) and runs the shared
    :func:`coord.agent.verify_merge_branch` primitive against the worktree the
    merge agent is sitting in.  This is the defense-in-depth twin of the
    coordinator-side gate in :func:`coord.interactive.finalize_interactive_exit`:
    same check, available to the agent before it self-reports.

    On thin-client machines (where the canonical board lives on a daemon) the
    board is fetched from the daemon automatically (#681).  As a last-resort
    fallback, supply ``--repo`` and ``--issue-number`` explicitly so the check
    can run even when the board lookup returns nothing.
    """
    from coord.agent import (  # noqa: PLC0415
        resolve_closed_issue_numbers,
        verify_merge_branch,
    )
    from coord.board_service import read_board  # noqa: PLC0415

    cfg = _load_config(config_path)
    board = read_board()
    work = board.find_by_id(work_aid)
    if work is None:
        if repo_opt and issue_number_opt is not None:
            # Thin-client fallback: the board lookup found nothing (empty local
            # DB or daemon didn't carry this aid), but the caller supplied the
            # known values explicitly via --repo / --issue-number (#681).
            repo_name = repo_opt
            issue_num = issue_number_opt
            branch_display = "(unknown)"
        else:
            click.echo(
                f"error: no assignment {work_aid!r} on the board "
                "(use the work id from `coord status`, or supply "
                "--repo and --issue-number as a fallback).",
                err=True,
            )
            sys.exit(2)
    else:
        repo_name = work.repo_name
        issue_num = int(work.issue_number)
        branch_display = work.branch or "(unknown)"

    repo_cfg = cfg.repo(repo_name)
    base = (repo_cfg.default_branch if repo_cfg else None) or "main"
    if repo_cfg is not None and repo_cfg.develop_branch:
        # #934: verify against `feature/ms-NN` when this issue belongs to a
        # milestone and the repo opted into the git model — falls back to
        # `default_branch` (above) for everything else.
        from coord.branch_model import (  # noqa: PLC0415
            fetch_issue_milestone_number,
            resolve_base_branch,
        )

        milestone_number = fetch_issue_milestone_number(repo_cfg.github, issue_num)
        base = resolve_base_branch(repo_cfg, milestone_number)
    repo_github = repo_cfg.github if repo_cfg else None
    wt_path = Path(path_opt).expanduser() if path_opt else Path.cwd()

    mv = verify_merge_branch(wt_path, base=base, issue_number=issue_num)
    # #1279: only worth a `gh` round-trip when the cheap git-only pass above
    # actually found blocking foreign commits — corroborate against GitHub's
    # closed-issue state and re-verify with the downgrade signal populated.
    closed = resolve_closed_issue_numbers(repo_github, mv.foreign, issue_num)
    if closed:
        mv = verify_merge_branch(
            wt_path, base=base, issue_number=issue_num, closed_issue_numbers=closed
        )

    click.echo(f"branch:        {branch_display}")
    click.echo(f"target base:   {base}")
    click.echo(f"{base}-ahead:   {mv.default_ahead}  (must be 0)")
    click.echo(f"adds {len(mv.added)} commit(s) over {base}:")
    advisory_set = set(mv.advisory_foreign)
    for sha, subj in mv.added:
        if (sha, subj) in mv.foreign:
            flag = " [FOREIGN — BLOCKING]"
        elif (sha, subj) in advisory_set:
            flag = " [advisory: references closed issue]"
        else:
            flag = ""
        click.echo(f"  {sha[:9]} {subj}{flag}")

    if mv.ok:
        click.echo("✓ merge-ready: base fully contained, no foreign commits.")
        note = mv.advisory_note()
        if note:
            click.echo(f"  {note}")
        return
    click.echo(f"✗ NOT merge-ready: {mv.block_summary(base)}", err=True)
    sys.exit(1)


@click.command(
    help=(
        "Bounce the pipeline back to Work after a review requested changes. "
        "Dispatches a fix worker that reads the reviewer's findings as its "
        "briefing and pushes corrections to the same branch."
    ),
)


@click.argument("review_assignment_id")
@_CONFIG_OPTION
def bounce(review_assignment_id: str, config_path: Path) -> None:
    """Manual trigger for the auto-loop's fix-dispatch path.

    `coord notify` already runs this automatically the first time a
    review completion is observed, but the auto-loop bails when the
    review log isn't reachable at that moment (remote agent offline /
    log pruned).  This command re-runs the same dispatch on demand —
    useful as a recovery path for the user and as the TUI's "Fix"
    button.
    """
    from coord.auto_loop import process_review_completion
    from coord.board_service import read_board, write_board
    from coord.state import COORD_DIR

    cfg = _load_config(config_path)
    board = read_board()

    review = board.find_by_id(review_assignment_id)
    if review is None:
        click.echo(
            f"error: assignment {review_assignment_id!r} not found in board",
            err=True,
        )
        sys.exit(1)
    if review.type != "review":
        click.echo(
            f"error: {review_assignment_id} is type={review.type!r}, not 'review'. "
            f"Pass the review assignment id, not the work assignment id.",
            err=True,
        )
        sys.exit(1)
    if review.review_verdict not in ("request-changes", None):
        click.echo(
            f"info: review verdict is {review.review_verdict!r} — only "
            f"'request-changes' triggers a fix dispatch. Nothing to do.",
            err=True,
        )
        sys.exit(1)

    # Try local log first; fall back to agent HTTP /logs when the
    # review ran on a remote machine and the file isn't on this
    # coordinator's filesystem.
    machine = next(
        (m for m in cfg.machines if m.name == review.machine_name), None,
    )
    machine_host = machine.host if machine and machine.host else None
    local_log = COORD_DIR / "logs" / f"{review_assignment_id}.log"
    log_path = str(local_log) if local_log.exists() else None

    actions = process_review_completion(
        review,
        board,
        cfg,
        log_path=log_path,
        machine_host=machine_host,
    )

    dispatched = any(a.kind == "fix_dispatched" for a in actions)
    # #522: terminal_skip mutates work.review_state="done" in
    # process_review_completion — persist it (same as the notify path) so the
    # row doesn't get re-evaluated, and treat it as a clean (not failed) exit.
    terminal = any(a.kind == "terminal_skip" for a in actions)
    if dispatched or terminal:
        write_board(board)

    for a in actions:
        click.echo(f"{a.kind}: {a.detail}")

    if not dispatched:
        # Distinguish clean outcomes (approve / already-merged-or-closed) from
        # genuine failure modes.
        if any(a.kind in ("approved", "terminal_skip") for a in actions):
            sys.exit(0)
        sys.exit(1)


@click.command(
    "reconcile-merges",
    help=(
        "Reconcile done work assignments against git/GitHub reality.\n\n"
        "Three conservative sweeps:\n"
        "  #611 — backfill a missing branch from a matching `issue-N-*` remote "
        "branch (a remote interactive session can finish done with branch=None, "
        "greying the TUI Start review/test/merge buttons);\n"
        "  #609 — flip work merged out-of-band (direct GitHub merge or a drained "
        "merge_queue row) to status='merged' so the TUI stops showing a grey "
        "merge box forever;\n"
        "  #721 — close open PRs whose work has already landed (issue closed or "
        "branch fully on the default branch) — review-PRs accumulate forever "
        "after squash merges otherwise.\n\n"
        "Acts only when certain; skips and explains otherwise."
    ),
)


@click.option("--repo", "repo_name", default=None, help="Only reconcile this repo.")
@click.option(
    "--dry-run", is_flag=True, help="Show what would change without writing."
)


@_CONFIG_OPTION
def reconcile_merges(repo_name: str | None, dry_run: bool, config_path: Path) -> None:
    """#609/#611: record out-of-band merges and backfill missing branches."""
    # #584: the canonical board + gh live on the daemon host, so on a thin
    # client this would sweep an empty local board and silently do nothing.
    # Route the whole operation to the daemon (mirrors `coord merge`).
    # COORD_RECONCILE_ON_DAEMON guards the daemon against re-routing to itself.
    from coord.board_service import daemon_reroute_target  # noqa: PLC0415

    _svc = daemon_reroute_target("COORD_RECONCILE_ON_DAEMON")
    if _svc is not None:
        _reconcile_via_daemon(_svc, {"repo": repo_name, "dry_run": dry_run})
        return

    from coord.reconcile import reconcile_board_merges
    from coord.state import build_board, save_board

    cfg = _load_config(config_path)
    board = build_board()
    actions = reconcile_board_merges(
        board, cfg, repo=repo_name, dry_run=dry_run
    )
    if not dry_run:
        save_board(board)
    if not actions:
        click.echo("Nothing to reconcile.")
        return
    for action in actions:
        click.echo(action)


@click.command(
    "post-pending-reviews",
    help=(
        "Post unposted review findings for done review assignments.\n\n"
        "Useful when a reviewer finished but notify didn't see the transition "
        "(e.g. agent reported 'cancelled', reap hung, or notify ran at the wrong time). "
        "Idempotent — already-posted findings are never re-posted."
    ),
)


@_CONFIG_OPTION
@click.option("--repo", "repo_name", default=None, help="Only process assignments for this repo.")
def post_pending_reviews(config_path: Path, repo_name: str | None) -> None:
    from coord.notify import post_orphaned_review_findings
    from coord.state import load_done_reviews_needing_post

    cfg = _load_config(config_path)

    candidates = load_done_reviews_needing_post(repo_name=repo_name)
    if not candidates:
        click.echo("No pending review assignments found.")
        return

    click.echo(f"Found {len(candidates)} review assignment(s) with unposted findings:")
    for row in candidates:
        aid = row["assignment_id"]
        click.echo(
            f"  {aid} — {row['repo_name']} #{row['issue_number']} "
            f"(machine: {row['machine_name']}, target: {row['review_target'] or 'n/a'})"
        )

    posted_ids = post_orphaned_review_findings(cfg, repo_name=repo_name)

    if not posted_ids:
        click.echo("\nNo findings posted (agents may be offline or logs unavailable).")
        return

    click.echo(f"\nPosted findings for {len(posted_ids)} assignment(s):")
    for aid in posted_ids:
        click.echo(f"  {aid}")

    still_pending = load_done_reviews_needing_post(repo_name=repo_name)
    if still_pending:
        click.echo(f"\n{len(still_pending)} assignment(s) still pending (logs not available):")
        for row in still_pending:
            click.echo(
                f"  {row['assignment_id']} — {row['repo_name']} #{row['issue_number']} "
                f"(machine: {row['machine_name']})"
            )


def _load_issue_states() -> tuple[dict[str, set[int]], dict[str, set[int]]]:
    """Return ``(open_by_repo, known_by_repo)``.

    - ``open_by_repo[repo]`` = set of issue numbers with state='open'.
    - ``known_by_repo[repo]`` = set of issue numbers with ANY state row in
      the cache.

    Used by the `coord merge` auto-enqueue path (#242).  Filter logic
    (in the caller) is permissive on cache misses:

    - issue in ``known_by_repo[repo]`` AND not in ``open_by_repo[repo]``
      → deny (we have explicit "closed" evidence)
    - otherwise → allow

    The earlier implementation denied any issue whose repo had ANY rows in
    the issues table but no row for the specific number — which silently
    skipped issues created after the cache's most-recent sync (we hit this
    when #278/#280 landed but the local cache stopped at #271).
    """
    try:
        from coord.db import get_connection

        conn = get_connection()
        rows = conn.execute(
            "SELECT repo_name, number, state FROM issues"
        ).fetchall()
    except Exception:  # noqa: BLE001 — caller treats empty as "unknown"
        return {}, {}

    open_by_repo: dict[str, set[int]] = {}
    known_by_repo: dict[str, set[int]] = {}
    for row in rows:
        repo_name = row[0]
        number = int(row[1])
        known_by_repo.setdefault(repo_name, set()).add(number)
        if row[2] == "open":
            open_by_repo.setdefault(repo_name, set()).add(number)
    return open_by_repo, known_by_repo


def _reconcile_via_daemon(svc, params: dict) -> None:
    """#584: run ``coord reconcile-merges`` on the daemon host (where the
    canonical DB lives + gh is authenticated) and relay its output, so the
    command does real work from a thin client instead of no-opping against an
    empty local board.  Reconcile is gh-bound but quick, hence the shorter
    timeout."""
    from coord.client import post_record  # noqa: PLC0415

    try:
        resp = post_record(svc, "/reconcile-merges", params, timeout=120.0)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: reconcile-merges via daemon failed: {exc}", err=True)
        sys.exit(1)
    output = resp.get("output") or ""
    if output:
        click.echo(output, nl=False)
    if resp.get("error"):
        click.echo(f"error: {resp['error']}", err=True)
    code = resp.get("exit_code") or 0
    if code:
        sys.exit(int(code))


def _print_merge_plan_entries(planned: list) -> None:
    """Print a list of PlannedMerge entries grouped by repo → target_branch."""
    if not planned:
        click.echo("Merge queue is empty (nothing to plan).")
        return
    _last_group: tuple[str, str] | None = None
    for _p in planned:
        _gkey = (_p.repo_name, _p.target_branch)
        if _gkey != _last_group:
            if _last_group is not None:
                click.echo("")
            click.echo(f"{_p.repo_name} → {_p.target_branch}")
            _last_group = _gkey
        _size_str = f"+{_p.size}" if _p.size is not None else "?"
        _status_str = _p.status
        if _p.reason:
            _status_str = f"{_p.status}   {_p.reason}"
        click.echo(
            f"  {_p.rank}. #{_p.issue_number}  {_size_str}   "
            f"{_status_str}     {_p.issue_title}"
        )


def _show_plan_from_daemon(
    svc,
    *,
    repo_filter: str | None,
    order: str | None,
) -> None:
    """#779-fix: display merge plan via /board — never touches /merge.

    Older daemons (≤v0.4.53 pre-#779) receive ``plan=True`` via ``/merge``
    but have no show_plan handler, so they fall through to a full live merge
    cycle with side effects.  The ``merge_plan`` field has been injected into
    ``/board`` since #776/v0.4.53, so we fetch that instead — guaranteed
    read-only on every supported daemon version.

    Exits with an error message if the daemon payload lacks ``merge_plan``
    (daemon predates v0.4.53); the caller should not fall through to a local
    path that would show an empty thin-client queue.
    """
    from coord.client import fetch_board_payload  # noqa: PLC0415
    from coord.merge_queue import PlannedMerge  # noqa: PLC0415

    try:
        payload = fetch_board_payload(svc)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: fetch board for --plan failed: {exc}", err=True)
        sys.exit(1)

    if "merge_plan" not in payload:
        click.echo(
            "error: daemon does not expose merge_plan in /board "
            "(upgrade the daemon to v0.4.53+ to use coord merge --plan).",
            err=True,
        )
        sys.exit(1)

    raw: list[dict] = payload.get("merge_plan") or []
    known = set(PlannedMerge.__dataclass_fields__)
    planned = [PlannedMerge(**{k: v for k, v in d.items() if k in known}) for d in raw]

    if repo_filter:
        planned = [p for p in planned if p.repo_name == repo_filter]

    if order:
        _override_ids = [s.strip() for s in order.split(",") if s.strip()]
        _by_id = {p.assignment_id: p for p in planned}
        _head = [_by_id[aid] for aid in _override_ids if aid in _by_id]
        _tail = [p for p in planned if p.assignment_id not in set(_override_ids)]
        planned = _head + _tail
        for _i, _p in enumerate(planned, 1):
            _p.rank = _i

    _print_merge_plan_entries(planned)


def _merge_via_daemon(svc, params: dict) -> None:
    """#584: run ``coord merge`` on the daemon host (where the canonical DB +
    merge queue + gh live) and relay its output, so the TUI 'Go' button and
    ``coord merge`` work from any thin client.  Merges can take minutes (PR
    creation, CI waits), hence the long timeout."""
    from coord.client import post_record  # noqa: PLC0415

    try:
        resp = post_record(svc, "/merge", params, timeout=900.0)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: merge via daemon failed: {exc}", err=True)
        sys.exit(1)
    output = resp.get("output") or ""
    if output:
        click.echo(output, nl=False)
    if resp.get("error"):
        click.echo(f"error: {resp['error']}", err=True)
    code = resp.get("exit_code") or 0
    if code:
        sys.exit(int(code))


@click.command(help="Process the merge queue: open PRs and merge in sequence.")
@_CONFIG_OPTION
@click.option("--dry-run", is_flag=True, help="Show the plan without opening or merging PRs.")
@click.option(
    "--plan",
    "show_plan",
    is_flag=True,
    help=(
        "#779: Print the ranked merge order and per-entry gate status. "
        "No PRs opened, no merges — purely read-only."
    ),
)


@click.option(
    "--order",
    default=None,
    help="Comma-separated assignment IDs to merge first (overrides size-based sequencing).",
)


@click.option("--repo", "repo_filter", default=None, help="Only process this repo's queue.")
@click.option(
    "--method",
    type=click.Choice(["rebase", "squash", "merge"]),
    default="rebase",
    show_default=True,
)


@click.option(
    "--force-merge",
    is_flag=True,
    help=(
        "Skip the CI check gate — merge even if checks failed or are still running. "
        "Also overrides the #1318 epic-closing-keyword guard: merge anyway even "
        "when a commit message on the branch contains a closing keyword targeting "
        "an epic (the epic WILL auto-close on GitHub)."
    ),
)


@click.option(
    "--skip-review",
    is_flag=True,
    help="Skip the review-approval gate — merge even when no approved review is on the board (#253).",
)


@click.option(
    "--skip-smoke",
    is_flag=True,
    help="Skip the interactive smoke-test gate — merge even when no smoke verdict is recorded (#465).",
)


@click.option(
    "--drop",
    "drop_assignment",
    default=None,
    metavar="ASSIGNMENT_ID",
    help=(
        "#732: Drop exactly one merge_queue entry by assignment_id. "
        "Routes through the daemon so thin clients don't need local DB access."
    ),
)


@click.option(
    "--only",
    "only_assignment",
    default=None,
    metavar="ASSIGNMENT_ID",
    help=(
        "#780: Merge exactly one entry by assignment_id, leaving the rest of the queue "
        "untouched.  Mutually exclusive with --order.  BLOCKED entries are reported "
        "and skipped (use --force-merge to override gates)."
    ),
)


@click.option(
    "--override-human-required",
    "override_human_required",
    default=None,
    metavar="REASON",
    help=(
        "#1251: explicit, audited override for a HUMAN_REQUIRED entry — clears the "
        "flag and requeues it as PENDING so this run's other gates (--skip-review, "
        "--skip-smoke, --force-merge) can still apply normally. Requires --only "
        "<assignment_id> and a reason string, which is written to the audit trail "
        "alongside the original conflict_human_required event. Distinct from "
        "--force-merge on purpose: human_required means an automated process already "
        "gave up on this entry, not just that a gate wasn't run."
    ),
)


def merge(
    config_path: Path,
    dry_run: bool,
    show_plan: bool,
    order: str | None,
    repo_filter: str | None,
    method: str,
    force_merge: bool,
    skip_review: bool,
    skip_smoke: bool,
    drop_assignment: str | None,
    only_assignment: str | None,
    override_human_required: str | None,
) -> None:
    # #1251: --override-human-required is a surgical single-entry override — it
    # only makes sense paired with --only, which pins down the one entry it
    # applies to.  Validate up front (before any daemon round-trip) so a thin
    # client fails fast instead of silently no-op'ing the flag on the daemon
    # side (only_assignment gates the block that actually consumes it below).
    #
    # #1251-review: both this check and the later `if override_human_required:`
    # gate treat an empty/whitespace-only reason as falsy, so
    # `--override-human-required ""` would otherwise skip *every* validation
    # and *every* effect — no error, no override, no audit row — leaving the
    # entry stuck HUMAN_REQUIRED with no feedback that the reason was
    # rejected.  Catch it explicitly first, before the --only check, since an
    # empty reason is invalid regardless of what else was passed.
    if override_human_required is not None and not override_human_required.strip():
        click.echo(
            "error: --override-human-required requires a non-empty reason string",
            err=True,
        )
        sys.exit(1)
    if override_human_required and not only_assignment:
        click.echo(
            "error: --override-human-required requires --only <assignment_id> — "
            "it targets exactly one entry, never a repo-wide scan",
            err=True,
        )
        sys.exit(1)

    # #584: the merge queue + board live in the canonical (host-local) DB, so on
    # a thin client `coord merge` (and the TUI 'Go' button, which shells out to
    # it) would silently no-op against an empty local board.  Route the whole
    # operation to the daemon — it runs the merge where the DB + gh live and
    # returns its output.  COORD_MERGE_ON_DAEMON guards the daemon against
    # re-routing to itself (it calls this same command with the env var set).
    from coord.board_service import daemon_reroute_target  # noqa: PLC0415

    _merge_svc = daemon_reroute_target("COORD_MERGE_ON_DAEMON")
    if _merge_svc is not None:
        # #779-fix: --plan must never reach /merge on an older daemon — it has
        # no show_plan handler and falls through to a live merge cycle (side
        # effects).  Route through /board instead; merge_plan has been in the
        # /board payload since #776/v0.4.53.
        if show_plan:
            _show_plan_from_daemon(_merge_svc, repo_filter=repo_filter, order=order)
            return
        _merge_via_daemon(_merge_svc, {
            "dry_run": dry_run, "order": order,
            "repo_filter": repo_filter, "method": method,
            "force_merge": force_merge, "skip_review": skip_review,
            "skip_smoke": skip_smoke, "drop": drop_assignment,
            "only": only_assignment,
            "override_human_required": override_human_required,
        })
        return

    # #732: --drop is a surgical single-entry removal; handle before the full
    # merge pipeline so it works even when the queue is otherwise busy/blocked.
    if drop_assignment:
        from coord import merge_queue as _mq  # noqa: PLC0415

        removed = _mq.drop_entry(drop_assignment)
        if removed:
            click.echo(f"merge-queue: dropped entry {drop_assignment}")
        else:
            click.echo(
                f"merge-queue: no entry found for {drop_assignment!r}", err=True
            )
            sys.exit(1)
        return

    # #779: --plan is a pure read-only path; handle it before the auto-enqueue
    # scan so it never causes side effects.  When a daemon is present this path
    # is short-circuited above by _show_plan_from_daemon (/board, not /merge).
    # This local branch runs on the daemon itself (COORD_MERGE_ON_DAEMON set)
    # or when no daemon is configured (standalone dev environment).
    if show_plan:
        from coord import merge_queue as _plan_mq  # noqa: PLC0415
        from coord.ci_store import build_ci_store as _build_ci_store  # noqa: PLC0415
        from coord.state import load_board as _load_board  # noqa: PLC0415

        _cfg = _load_config(config_path)
        _board = _load_board()
        _ci = _build_ci_store(_cfg.ci_store.type)

        planned = _plan_mq.plan(_board, _cfg, _ci)

        # --repo scoping
        if repo_filter:
            planned = [p for p in planned if p.repo_name == repo_filter]

        # --order: put the named IDs first, then renumber ranks so the display
        # matches what a subsequent `coord merge --order <ids>` would actually do.
        if order:
            _override_ids = [s.strip() for s in order.split(",") if s.strip()]
            _by_id = {p.assignment_id: p for p in planned}
            _head = [_by_id[aid] for aid in _override_ids if aid in _by_id]
            _tail = [p for p in planned if p.assignment_id not in set(_override_ids)]
            planned = _head + _tail
            for _i, _p in enumerate(planned, 1):
                _p.rank = _i

        _print_merge_plan_entries(planned)
        return

    from coord import github_ops as gh_ops
    from coord import merge_queue as mq
    from coord.ci_store import build_ci_store
    from coord.merge_queue import CONFLICT, HUMAN_REQUIRED, PENDING
    from coord.state import load_board

    # #780: --only is a surgical single-entry merge that leaves all other queue
    # entries in PENDING state.  Handled early — before the full auto-enqueue
    # scan — so a --only run doesn't touch unrelated entries.
    if only_assignment:
        if order:
            click.echo(
                "error: --only and --order are mutually exclusive", err=True
            )
            sys.exit(1)
        cfg_only = _load_config(config_path)
        only_queue = mq.load_queue()
        only_entry = next(
            (e for e in only_queue if e.assignment_id == only_assignment), None
        )
        if only_entry is None:
            click.echo(
                f"merge-queue: no entry found for {only_assignment!r}", err=True
            )
            sys.exit(1)
        # #1251: --override-human-required is the explicit, audited escape
        # hatch for an entry an automated conflict-fix (or a permission /
        # branch-protection classification) already gave up on.  It's a
        # different class of override from --skip-smoke/--skip-review/
        # --force-merge — those waive a gate that simply wasn't run; this
        # clears a flag that says "automation gave up, a human must decide" —
        # so it gets its own flag, its own validation, and its own audit
        # row, never bundled into --force-merge.
        if override_human_required:
            if only_entry.state != HUMAN_REQUIRED:
                click.echo(
                    "error: --override-human-required only applies to a "
                    f"HUMAN_REQUIRED entry; {only_assignment!r} is in state "
                    f"{only_entry.state!r}",
                    err=True,
                )
                sys.exit(1)
            if dry_run:
                click.echo(
                    "  --override-human-required: (dry run) would clear "
                    f"HUMAN_REQUIRED on {only_assignment!r} — "
                    f"{override_human_required!r}"
                )
            else:
                from coord.audit import record_audit  # noqa: PLC0415

                record_audit(
                    tier="business",
                    category="merge",
                    event_type="human_required_override",
                    actor="user",
                    summary=(
                        f"human_required override: {only_entry.repo_name}"
                        f"#{only_entry.issue_number} ({only_assignment}) — "
                        f"{override_human_required}"
                    ),
                    repo=only_entry.repo_name,
                    issue=only_entry.issue_number,
                    assignment_id=only_entry.assignment_id,
                    details={"reason": override_human_required},
                )
                click.echo(
                    "  --override-human-required: cleared HUMAN_REQUIRED on "
                    f"{only_assignment!r} — {override_human_required!r} — "
                    "requeued as PENDING"
                )
            # Reset in-memory state either way so the dry-run event stream
            # below reflects what a real run would do (matching the review/
            # smoke gate dry-run convention); actual persistence is still
            # gated on `not dry_run` in the save block further down.
            only_entry.state = PENDING
            only_entry.error = None
        if only_entry.state != PENDING:
            click.echo(
                f"merge-queue: entry {only_assignment!r} is in state "
                f"{only_entry.state!r} (not PENDING) — cannot merge",
                err=True,
            )
            sys.exit(1)
        # #821: never pass None to process() — use an empty board so
        # has_smoke_verdict can apply its "no work found → fail open" rule.
        # process() blocks on board=None when a gate IS required; an empty
        # board lets the gate function decide.
        from coord.models import Board as _Board  # noqa: PLC0415
        _raw_board_only = load_board()
        board_only = _raw_board_only if _raw_board_only is not None else _Board(active=[], completed=[])
        ci_store_only = build_ci_store(cfg_only.ci_store.type)
        if skip_review:
            click.echo("  --skip-review: review-approval gate bypassed (#253)")
        if skip_smoke:
            click.echo("  --skip-smoke: interactive smoke-test gate bypassed (#465)")
        only_items = [only_entry]
        events_only = mq.process(
            only_items, gh_ops,
            method=method, dry_run=dry_run, presorted=True,
            ci_store=ci_store_only, force_merge=force_merge,
            config=cfg_only, board=board_only,
            skip_review=skip_review, skip_smoke=skip_smoke,
        )
        for ev in events_only:
            e = ev.entry
            prefix = f"  {e.repo_name} #{e.issue_number} ({e.branch})"
            click.echo(f"{prefix}: {ev.kind} — {ev.message}")
        if not dry_run:
            # Save only the modified entry back; all other entries are untouched.
            all_items_only = mq.load_queue()
            by_id_only = {only_entry.assignment_id: only_entry}
            merged_only = [by_id_only.get(x.assignment_id, x) for x in all_items_only]
            mq.save_queue(merged_only)
        click.echo("")
        click.echo(
            "Summary (--only): "
            + ", ".join(f"{k}={v}" for k, v in sorted(
                {x.state: 1 for x in only_items}.items()
            ))
        )
        return

    cfg = _load_config(config_path)

    # #242: Before processing, scan board.completed for done work assignments
    # that should be queued but aren't.  Without this, `coord merge` silently
    # no-ops when a work assignment reached "done" via a path that didn't
    # also trigger the `coord status` enqueue hook (restart, notify-driven
    # mark_done, etc.).  enqueue() is idempotent — by assignment_id — so this
    # is safe to call on every invocation.
    #
    # Filter on issue.state == 'open': a closed issue was almost certainly
    # already merged externally (or won't-fix'd) and re-attempting a merge
    # for it would open spurious PRs against branches that may not even
    # exist anymore.  When the issues table has no row for an issue (cache
    # miss), default to OPEN — that matches the prior coord status enqueue
    # path which had no such check.
    # #821: never pass None to process() — use an empty board so
    # has_smoke_verdict can apply its "no work found → fail open" rule.
    # process() blocks on board=None when a gate IS required; an empty
    # board lets the gate function decide.
    from coord.models import Board as _Board  # noqa: PLC0415
    _raw_board = load_board()
    board = _raw_board if _raw_board is not None else _Board(active=[], completed=[])
    open_by_repo, known_by_repo = _load_issue_states()

    auto_enqueued: list[str] = []
    # Per-repo cache of branches that still exist on origin.  Lets us skip
    # re-enqueuing done-work whose branch was already merged-and-deleted — the
    # dominant merge-queue clog source.  A done assignment for a closed issue
    # often isn't in the open-only issues cache, so the issue-state filter
    # above misses it; branch-existence catches every merge path (coord merge,
    # gh pr merge, manual) uniformly.  Fail OPEN on lookup failure.
    from coord import github_ops as _gho
    branch_cache: dict[str, set[str]] = {}
    # #525: per-run cache for work_is_terminal; shared across the whole
    # auto-enqueue loop so one gh round-trip covers every repeated
    # (repo, issue, branch) triple.
    terminal_cache: dict = {}
    # #934: per-run cache for the issue -> milestone-number lookup, mirroring
    # terminal_cache above.
    milestone_cache: dict = {}
    if board is not None:
        for a in board.completed:
            if a.type not in WORK_LIKE_TYPES or a.status != "done":
                continue
            if not a.branch or not a.assignment_id:
                continue
            if repo_filter and a.repo_name != repo_filter:
                continue
            repo_cfg = cfg.repo(a.repo_name)
            if repo_cfg is None:
                continue
            # Issue-state filter: skip closed issues (probably merged elsewhere).
            # We deny only when the cache has explicit evidence the issue is
            # closed — i.e. there's a row for this (repo, number) and its
            # state isn't 'open'.  If the cache simply has no row for this
            # issue (e.g. it was created after the last sync), treat as
            # unknown and allow — denying on cache miss silently skipped
            # post-sync issues (#278/#280 hit this).
            known_issues = known_by_repo.get(a.repo_name, set())
            open_issues = open_by_repo.get(a.repo_name, set())
            if a.issue_number in known_issues and a.issue_number not in open_issues:
                continue
            # Skip work whose branch no longer exists on origin (already
            # merged + deleted).  Fail OPEN: only skip when we got a real
            # (non-empty) branch list back and the branch isn't in it.
            origin_branches = branch_cache.get(a.repo_name)
            if origin_branches is None:
                origin_branches = _gho.list_remote_branch_names(repo_cfg.github)
                branch_cache[a.repo_name] = origin_branches
            if origin_branches and a.branch not in origin_branches:
                continue
            # #525: never enqueue work that is already done on GitHub —
            # issue closed OR PR merged.  Mirrors the #522 guard in
            # review.dispatch_review.  Fail OPEN: a transient gh error
            # must never block a real enqueue.
            if _gho.work_is_terminal(
                repo_cfg.github, a.issue_number, a.branch,
                cache=terminal_cache,
            ):
                continue
            # #946: review + smoke gates, via the shared predicate — this
            # loop was the primary ungated enqueue path (#782/#795 reached
            # the merge queue with a failed test / no review at all).  Mirrors
            # the gate already enforced by the daemon's
            # `enqueue_approved_work` so `coord merge` and the passive tick
            # agree on what's allowed to queue.
            if not mq.passes_merge_gates(a, cfg, board):
                continue
            # #736 / #292: use refresh_entry_assignment (not bare enqueue) so
            # an existing PENDING entry is re-keyed to the latest fix assignment
            # when the original assignment_id no longer matches.  Dedup by
            # (repo_github, branch) is preserved — refresh_entry_assignment is a
            # no-op when the entry is already correctly keyed.
            # #934: target `feature/ms-NN` when this issue belongs to a
            # milestone and the repo opted into the git model — the
            # milestone lookup itself is skipped (no `gh` call) when it
            # hasn't, falling back to `default_branch` unchanged.
            target_branch = repo_cfg.default_branch
            if repo_cfg.develop_branch:
                from coord.branch_model import (  # noqa: PLC0415
                    fetch_issue_milestone_number,
                    resolve_base_branch,
                )

                milestone_number = fetch_issue_milestone_number(
                    repo_cfg.github, a.issue_number, cache=milestone_cache,
                )
                target_branch = resolve_base_branch(repo_cfg, milestone_number)

            if mq.refresh_entry_assignment(
                a,
                repo_github=repo_cfg.github,
                target_branch=target_branch,
            ):
                auto_enqueued.append(
                    f"  auto-enqueued: {a.repo_name} #{a.issue_number} "
                    f"({a.branch} → {target_branch})"
                )
    for line in auto_enqueued:
        click.echo(line)

    items = mq.load_queue()
    if repo_filter:
        items = [x for x in items if x.repo_name == repo_filter]
    if not items:
        # Distinguish "nothing in the queue" from "nothing to do because
        # there's no completed work to merge" — the latter is the common
        # case before #242 was fixed and was the silent-fail symptom.
        if board is not None and any(
            a.type in WORK_LIKE_TYPES and a.status == "done" and a.branch
            for a in board.completed
            if (not repo_filter or a.repo_name == repo_filter)
        ):
            click.echo("Merge queue is empty (all done-work is already merged or has no branch).")
        else:
            click.echo("Merge queue is empty (no completed work to merge).")
        return

    presorted = False
    if order:
        ids = [s.strip() for s in order.split(",") if s.strip()]
        items = mq.reorder(items, ids)
        presorted = True

    pending = [x for x in items if x.state == PENDING]
    if not pending:
        # Still surface terminal states so the user knows what happened.
        for x in items:
            click.echo(f"  [{x.state}] {x.repo_name} #{x.issue_number} ({x.branch})")
        return

    ci_store = build_ci_store(cfg.ci_store.type)
    if skip_review:
        click.echo("  --skip-review: review-approval gate bypassed (#253)")
    if skip_smoke:
        click.echo("  --skip-smoke: interactive smoke-test gate bypassed (#465)")
    events = mq.process(
        items, gh_ops,
        method=method, dry_run=dry_run, presorted=presorted,
        ci_store=ci_store, force_merge=force_merge,
        config=cfg, board=board, skip_review=skip_review, skip_smoke=skip_smoke,
    )

    for ev in events:
        e = ev.entry
        prefix = f"  {e.repo_name} #{e.issue_number} ({e.branch})"
        click.echo(f"{prefix}: {ev.kind} — {ev.message}")

    # #241: classify any conflict events and dispatch a conflict-fix worker
    # for the eligible ones.  Mutates ev.entry.state in place — ev.entry IS
    # items[i] from process() — so the final save block below picks up
    # HUMAN_REQUIRED naturally without a separate save_queue call.
    conflict_events = [ev for ev in events if ev.kind == "conflict"]
    if conflict_events and not dry_run:
        from coord.audit import record_audit
        from coord.conflict_fix import dispatch_conflict_fix, has_prior_conflict_fix
        from coord.merge_queue import HUMAN_REQUIRED, classify_conflict
        from coord.state import load_board, save_board

        fix_board = load_board()
        if fix_board is not None:
            dispatched_any = False
            for ev in conflict_events:
                kind = classify_conflict(ev.entry.error)
                if kind == "rebaseable":
                    # Retry cap (#241/#784): if a conflict-fix already ran and
                    # failed for this entry in this session, don't loop — mark
                    # HUMAN_REQUIRED so the user takes over.  A successful
                    # prior fix does not trigger this guard (#784).
                    if has_prior_conflict_fix(fix_board, ev.entry.assignment_id):
                        ev.entry.state = HUMAN_REQUIRED
                        click.echo(
                            f"  {ev.entry.repo_name} #{ev.entry.issue_number}: "
                            "conflict-fix retry cap hit — manual resolution required"
                        )
                        # #1038: the coordinator's own retry-cap logic made
                        # this call, not the human running `coord merge` —
                        # operational tier, same as the other automatic
                        # conflict-classification outcomes below.
                        record_audit(
                            tier="operational",
                            category="merge",
                            event_type="conflict_human_required",
                            actor="daemon",
                            summary=f"conflict-fix retry cap hit: "
                            f"{ev.entry.repo_name}#{ev.entry.issue_number} — "
                            "manual resolution required",
                            repo=ev.entry.repo_name,
                            issue=ev.entry.issue_number,
                            assignment_id=ev.entry.assignment_id,
                            details={"reason": "retry_cap"},
                        )
                        continue
                    fix = dispatch_conflict_fix(
                        ev.entry,
                        fix_board,
                        cfg,
                        prefer_machine=_machine_for_assignment(
                            fix_board, ev.entry.assignment_id,
                        ),
                    )
                    if fix is not None:
                        click.echo(
                            f"  {ev.entry.repo_name} #{ev.entry.issue_number}: "
                            f"conflict-fix dispatched to {fix.machine_name}"
                        )
                        dispatched_any = True
                    else:
                        click.echo(
                            f"  {ev.entry.repo_name} #{ev.entry.issue_number}: "
                            "conflict-fix not dispatched (no machine / already in flight)"
                        )
                elif kind == "human":
                    ev.entry.state = HUMAN_REQUIRED
                    click.echo(
                        f"  {ev.entry.repo_name} #{ev.entry.issue_number}: "
                        "permission/protection error — manual resolution required"
                    )
                    record_audit(
                        tier="operational",
                        category="merge",
                        event_type="conflict_human_required",
                        actor="daemon",
                        summary=f"conflict classified non-rebaseable: "
                        f"{ev.entry.repo_name}#{ev.entry.issue_number} — "
                        "manual resolution required",
                        repo=ev.entry.repo_name,
                        issue=ev.entry.issue_number,
                        assignment_id=ev.entry.assignment_id,
                        details={"reason": "permission_or_protection"},
                    )
            if dispatched_any:
                save_board(fix_board)

    # Save state only when we actually moved
    if not dry_run:
        # Persist the updated entries by merging back over the on-disk queue.
        all_items = mq.load_queue()
        by_id = {x.assignment_id: x for x in items}
        merged = [by_id.get(x.assignment_id, x) for x in all_items]
        mq.save_queue(merged)

    # Summary
    states: dict[str, int] = {}
    for x in items:
        states[x.state] = states.get(x.state, 0) + 1
    click.echo("")
    click.echo(
        "Summary: "
        + ", ".join(f"{k}={v}" for k, v in sorted(states.items()))
    )
    if states.get(CONFLICT):
        click.echo("note: at least one PR has a conflict — resolve manually, then re-run.")