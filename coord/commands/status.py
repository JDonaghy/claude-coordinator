"""`coord status`/`usage`/`show-plan`/`diagnose` — read-only board and
machine reporting. Extracted from coord/cli.py (#747)."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

from coord import __version__, github_ops

from coord.commands._common import _CONFIG_OPTION, _load_config

if TYPE_CHECKING:
    from coord.config import Config


def _live_advisory_entries(
    entries: list[dict],
    cfg: "Config",
    *,
    cache: dict | None = None,
) -> list[dict]:
    """Drop advisory entries (#448) whose work is already terminal on GitHub.

    #1472: an advisory entry is agent-local state served verbatim from the
    worker's own completed-assignment map on every ``/status`` poll — nothing
    tells the agent the issue closed or the branch merged, so a genuinely
    resolved advisory (e.g. rescued, reviewed, and merged by hand) keeps
    showing "The work is UNVERIFIED — review it before testing or merging."
    forever. That trains the operator to skim past the whole advisory block,
    which is exactly where a real 0-commit rescue needs to be noticed.

    Reuses the shared #522 chokepoint guard
    (:func:`coord.github_ops.work_is_terminal` — "issue closed OR PR merged")
    rather than a second ad hoc check. **Fail-open**: an entry whose repo
    isn't in *cfg* (or has no ``github`` slug configured) is kept rather than
    silently dropped — ``work_is_terminal`` itself already fails open on any
    GitHub/CLI error. *cache* defaults to a dict scoped to this call so a
    repeated ``(repo, issue, branch)`` triple across entries costs one ``gh``
    round-trip, not one per entry — this list renders on every ``coord
    status``.
    """
    if cache is None:
        cache = {}
    live = []
    for e in entries:
        spec = e.get("spec") or {}
        repo_name = spec.get("repo_name")
        repo_cfg = cfg.repo(repo_name) if repo_name else None
        if repo_cfg is None or not repo_cfg.github:
            live.append(e)
            continue
        if github_ops.work_is_terminal(
            repo_cfg.github,
            spec.get("issue_number"),
            e.get("branch"),
            cache=cache,
        ):
            continue
        live.append(e)
    return live


@click.command(help="Show all machines, assignments, and connectivity.")
@_CONFIG_OPTION
@click.option("--machine", "machine_filter", default=None, help="Only show this machine.")
@click.option("--timeout", default=3.0, show_default=True, type=float, help="Per-machine health-check timeout (seconds).")
@click.option("--no-reconcile", is_flag=True, help="Skip auto-reconciliation of the board with live agent state.")
@click.option(
    "--freshness",
    is_flag=True,
    help="Also report per-machine repo freshness vs GitHub HEADs.",
)


def status(config_path: Path, machine_filter: str | None, no_reconcile: bool, timeout: float, freshness: bool) -> None:
    from coord import freshness as fresh
    from coord.deps import blocked_repos as compute_blocked, build_dep_graph
    from coord.board_service import read_board, write_board
    from coord.client import resolve_board_service
    from coord.network import check_all, fetch_repos, fetch_status
    from coord.state import load_dispatched, load_notified

    # #584/#1080: when a board service is configured, read the board + config
    # from the daemon instead of local SQLite. _load_config() itself now always
    # fetches the daemon's config on a thin client (never trusts a local file
    # that happens to exist — the config-fetch pre-step that used to live here
    # was a redundant duplicate of that same buggy "local file exists" check,
    # removed in #1080). `svc` below is still needed to gate the local-only
    # reads (queue/notified/session) further down. Unset ⇒ unchanged local
    # behaviour.
    svc = resolve_board_service()
    cfg = _load_config(config_path)

    # Dependency graph (only when --machine isn't narrowing the view).
    if not machine_filter:
        graph = build_dep_graph(cfg.repos)
        if any(deps for deps in graph.values()):
            click.echo("Dependency graph:")
            for repo in cfg.repos:
                deps = graph.get(repo.name, [])
                if deps:
                    click.echo(f"  {repo.name} → {', '.join(deps)}")
                else:
                    click.echo(f"  {repo.name} (no dependencies)")
            click.echo()

    machines = cfg.machines
    if machine_filter:
        machines = [m for m in machines if m.name == machine_filter]
        if not machines:
            click.echo(
                f"error: machine {machine_filter!r} not in coordinator.yml "
                f"(have: {[m.name for m in cfg.machines]})",
                err=True,
            )
            sys.exit(2)

    # #1563: paused_set() is daemon-aware — on a thin client it fetches the
    # daemon's own `/pause` copy, so this renders the state that actually
    # governs dispatch instead of a host-local file the daemon never reads.
    from coord.machine_pause import paused_set  # noqa: PLC0415
    paused = paused_set()

    statuses = check_all(machines, timeout=timeout)
    agent_completed: dict[str, dict] = {}
    click.echo("Machines:")
    for s in statuses:
        m = s.machine
        latency = f" ({s.latency_ms:.0f}ms)" if s.latency_ms is not None else ""
        if s.is_online:
            status_result = fetch_status(m, timeout=timeout)
            if status_result.ok:
                active = (status_result.data or {}).get("active", [])
                if active:
                    a = active[0]
                    spec = a.get("spec", {})
                    spec_type = spec.get("type", "work")
                    badge_map = {"review": "[review] ", "smoke": "[smoke] ", "plan": "[plan] "}
                    badge = badge_map.get(spec_type, "")
                    target = spec.get("review_target")
                    if spec_type == "review" and target:
                        target_str = f" reviewing PR #{target}"
                    elif spec_type == "smoke" and target:
                        target_str = f" smoking branch `{target}`"
                    else:
                        target_str = ""
                    detail = (
                        f"busy — {badge}#{spec.get('issue_number', '?')}: "
                        f"{spec.get('issue_title', '?')}{target_str}"
                    )
                else:
                    detail = "idle"
            else:
                active = []
                detail = f"status unavailable ({status_result.error})"
            if status_result.ok and status_result.data:
                for entry in status_result.data.get("completed", []):
                    eid = entry.get("id") or entry.get("assignment_id")
                    if eid:
                        agent_completed[eid] = entry
            label = f"{s.state} • {detail}{latency}"
        else:
            status_result = None
            label = f"{s.state} — {s.reason}{latency}"

        # #1563: surface pause state the same way regardless of whether
        # `paused_set()` resolved it locally or from the daemon — a thin
        # client that just ran `coord pause` needs to SEE that it took,
        # otherwise a pause that silently didn't reach the daemon looks
        # identical to one that did (the whole bug this closes).
        if m.name in paused:
            label = f"PAUSED — {label}"

        # Extract agent version from /status response (added in #104).
        agent_version: str | None = None
        if status_result and status_result.ok and status_result.data:
            agent_version = status_result.data.get("version")

        repos = ", ".join(m.repos) if m.repos else "(none)"
        click.echo(f"  {m.name:15s} [{label}]")
        version_line = ""
        if agent_version:
            if agent_version != __version__:
                version_line = f"  agent-version: {agent_version} ⚠ (coord is {__version__})"
            else:
                version_line = f"  agent-version: {agent_version}"
        click.echo(f"    host: {m.host}  repos: {repos}{version_line}")

        if status_result and status_result.ok and status_result.data:
            for entry in status_result.data.get("active", []):
                progress = entry.get("progress")
                if not progress:
                    continue
                if progress.get("stuck"):
                    click.echo(f"    !! STUCK: {progress['stuck']}")
                for w in progress.get("warnings", []):
                    click.echo(f"    !! {w}")
                updates = progress.get("updates", [])
                if updates:
                    click.echo(f"    latest: {updates[-1]}")

    # Reconcile board with live agent data
    board = read_board()
    if not no_reconcile and agent_completed:
        # #749: write_board() routes to the daemon's /board upsert when a
        # board service is configured, so a thin client's reconciliation now
        # actually lands on the shared DB instead of being skipped entirely.
        reconciled = 0
        for a in board.active[:]:
            if a.assignment_id is None:
                continue
            entry = agent_completed.get(a.assignment_id)
            if entry is None:
                continue
            branch = entry.get("branch")
            agent_status = entry.get("status")
            if agent_status == "done":
                board.mark_done_by_id(
                    a.assignment_id,
                    finished_at=entry.get("finished_at"),
                    branch=branch,
                )
            elif agent_status == "advisory":
                # #448: 0-commit clean exit — treat as done on the board so
                # the assignment doesn't block; the advisory section below
                # flags it for human attention.  Mirror reconcile.py: set
                # status="advisory" (mark_done_by_id leaves it as "done")
                # and review_state="advisory" on work assignments so that
                # the review-dispatch loop in coord notify skips them.
                done = board.mark_done_by_id(
                    a.assignment_id,
                    finished_at=entry.get("finished_at"),
                    branch=branch,
                )
                if done is not None:
                    done.status = "advisory"
                    if done.type == "work":
                        done.review_state = "advisory"
            else:
                board.mark_failed_by_id(
                    a.assignment_id,
                    finished_at=entry.get("finished_at"),
                )
            # #1461: stamp a usage-limit-kill diagnostic (if the agent
            # flagged one — AgentServer._reap, agent.py) onto the persisted
            # `failure_reason` column. Routed through the already-daemon-aware
            # `set_assignment_failure_reason` (#618) rather than a raw
            # get_connection() write — `coord status` is thin-client
            # reachable, and a raw local write would silently land on the
            # thin client's empty local DB instead of the daemon's (the same
            # #906 audit gap `get_issue_test_mode` was fixed for). This also
            # normalises status to 'failed' even when the branch above set
            # 'advisory' — a usage-limit kill is the one terminal state known
            # safe to re-dispatch unchanged (drive.py's FAILED bucket),
            # mirrors coord.reconcile._record_usage_limit_reason exactly.
            usage_limit_reason = entry.get("usage_limit_reason")
            if usage_limit_reason and a.assignment_id:
                try:
                    from coord.state import set_assignment_failure_reason

                    set_assignment_failure_reason(a.assignment_id, usage_limit_reason)
                except Exception:  # noqa: BLE001 — diagnostic only
                    pass
            reconciled += 1
        if reconciled:
            write_board(board)
            click.echo(f"\n  (reconciled {reconciled} assignment(s) from live agent data)")

    # #1461: surface usage-limit kills as a distinct fleet-level condition —
    # a known-safe-to-retry-once-reset wait, not a defect. Shown ahead of (and
    # excluded from) the Advisory/plain-failure buckets below so a confusing
    # evening reads as "3 killed by the usage limit, resets 8:30pm" instead of
    # N unrelated-looking advisory/failed rows.
    usage_limit_entries = [
        e for e in agent_completed.values()
        if e.get("usage_limit_reason")
    ]
    if usage_limit_entries:
        click.echo("")
        click.echo(
            "⏳ Usage limit (worker killed by the account's usage limit — "
            "safe to retry unchanged once reset):"
        )
        for e in usage_limit_entries:
            spec = e.get("spec", {})
            click.echo(
                f"  #{spec.get('issue_number', '?')}: "
                f"{spec.get('issue_title', '?')} "
                f"[{spec.get('repo_name', '?')}]  — {e['usage_limit_reason']}"
            )

    # #448: surface advisory assignments (0 commits, clean exit) so the
    # operator knows they need attention without having to dig into logs.
    # Usage-limit kills are excluded — surfaced separately above, since they
    # are a wait condition rather than something needing human attention.
    #
    # #1472: an advisory entry is agent-local state — it lives in the
    # worker's own completed-assignment map (`_COMPLETED_HISTORY_CAP` prunes
    # it by *count*, not by GitHub outcome) — so it keeps being re-served
    # here forever, even after the issue closes or the branch merges out
    # from under it. Re-check terminal state on every render rather than
    # trusting the agent to have cleared it.
    #
    # Both filters apply: #1461 drops usage-limit kills (a wait condition,
    # shown above) and #1472 drops work that has since gone terminal.
    advisory_entries = _live_advisory_entries(
        [
            e for e in agent_completed.values()
            if e.get("status") == "advisory" and not e.get("usage_limit_reason")
        ],
        cfg,
    )
    if advisory_entries:
        click.echo("")
        click.echo("⚠ Advisory (needs attention — worker exited cleanly with 0 commits):")
        for e in advisory_entries:
            spec = e.get("spec", {})
            reason = e.get("zero_commit_reason") or "0 commits pushed"
            click.echo(
                f"  #{spec.get('issue_number', '?')}: "
                f"{spec.get('issue_title', '?')} "
                f"[{spec.get('repo_name', '?')}]  — {reason}"
            )

    blocked = compute_blocked(cfg.repos, board.active)
    if blocked:
        click.echo("")
        click.echo("Blocked repos:")
        for repo_name, reasons in blocked.items():
            click.echo(f"  {repo_name}:")
            for reason in reasons:
                click.echo(f"    - {reason}")

    if freshness:
        click.echo("")
        click.echo("Repo freshness:")
        github_heads: dict[str, str | None] = {}
        for repo_cfg in cfg.repos:
            try:
                github_heads[repo_cfg.name] = github_ops.get_default_branch_head(
                    repo_cfg.github, repo_cfg.default_branch
                )
            except RuntimeError as e:
                github_heads[repo_cfg.name] = None
                click.echo(f"  (github HEAD lookup failed for {repo_cfg.name}: {e})", err=True)
        for s in statuses:
            if not s.is_online:
                click.echo(f"  {s.machine.name}: (offline, skipping)")
                continue
            agent_repos = fetch_repos(s.machine, timeout=timeout) or {}
            click.echo(f"  {s.machine.name}:")
            for repo_name in s.machine.repos:
                rf = fresh.compare(repo_name, agent_repos.get(repo_name), github_heads.get(repo_name))
                local = (rf.local_sha or "?")[:7]
                remote = (rf.remote_sha or "?")[:7]
                tag = f"[{rf.state}]"
                detail = f"local {local} remote {remote}"
                if rf.dirty:
                    detail += " (dirty)"
                if rf.error:
                    detail += f" — {rf.error}"
                click.echo(f"    {repo_name:20s} {tag:10s} {detail}")

    # Merge queue
    from coord import merge_queue as mq

    # #584: merge_queue lives in the (host-local) DB; skip it for a thin client.
    queue = [] if svc else mq.load_queue()
    by_repo = mq.pending_summary(queue) if queue else {}
    if by_repo:
        click.echo("")
        click.echo("Merge queue:")
        for repo_name, entries in sorted(by_repo.items()):
            click.echo(f"  {repo_name}:")
            for e in entries:
                size = f"+{e.size}" if e.size is not None else "?"
                pr = f"PR #{e.pr_number}" if e.pr_number else "no PR yet"
                tag = f"[{e.state}]"
                line = f"    {tag:11s} #{e.issue_number} ({e.branch} → {e.target_branch}) {pr} size={size}"
                click.echo(line)
                # #420: recompute the review/smoke gate error live rather than
                # echoing the stored string verbatim — it's only refreshed on
                # a real merge attempt, so an approval/verdict that landed
                # since then would otherwise show as stale as "blocked".
                live_error = mq.display_error(e, board, cfg)
                if live_error:
                    click.echo(f"      error: {live_error}")

    # #920: sibling-overlap warnings — approved (PENDING) queue entries that
    # touch overlapping files and have been aging.  Mirrors the merge-queue
    # skip above: the queue is host-local, so this is a no-op on a thin
    # client (use `coord merge --plan`, which fetches the daemon-computed
    # equivalent via /board, from a thin client instead).
    if not svc:
        from coord.commands.merge import _print_sibling_overlap_warnings

        overlaps = mq.find_sibling_overlaps(board, cfg)
        _print_sibling_overlap_warnings(overlaps)

    # Auto-loop iteration-cap blockers: assignments where the review→fix loop
    # exhausted all allowed iterations without receiving an approval.  These
    # require manual intervention (bump pipeline.max_review_iterations or
    # dispatch a fix with `coord assign`) and are shown prominently so the
    # operator notices them on the first `coord status` after the cap fires.
    cap_hit_blocked = [
        a for a in board.completed
        if a.type == "work" and a.review_state == "cap_hit"
    ]
    if cap_hit_blocked:
        click.echo("")
        click.echo("⚠ Auto-loop blockers (manual action required):")
        for a in cap_hit_blocked:
            click.echo(
                f"  #{a.issue_number}: {a.issue_title} ({a.repo_name})"
                f"  [iteration cap hit]"
            )
            click.echo(
                f"    Options: bump pipeline.max_review_iterations in coordinator.yml"
                f" or 'coord assign' to dispatch a fix manually,"
                f" or 'coord merge --force-merge' to merge as-is."
            )

    # #586: branch-not-on-remote blockers — work that completed but the branch
    # was never pushed.  Downstream review/fix dispatch is blocked until the
    # operator pushes from the original worker machine.
    branch_not_pushed = [
        a for a in board.completed
        if a.type == "work" and a.review_state == "branch_not_on_remote"
    ]
    if branch_not_pushed:
        click.echo("")
        click.echo("⚠ Push required (review blocked — branch not on remote):")
        for a in branch_not_pushed:
            click.echo(
                f"  #{a.issue_number}: {a.issue_title} ({a.repo_name})"
                f"  [branch not on remote]"
            )
            click.echo(
                f"    Branch '{a.branch}' exists only on {a.machine_name}."
                f" Push it with: ssh {a.machine_name} 'cd <repo-path> && git push origin {a.branch}'"
                f" then re-run 'coord notify' to retry review dispatch."
            )

    # #904: no-eligible-reviewer blockers — every configured candidate machine
    # definitively rejected the review dispatch (drifted coordinator.yml vs.
    # an agent's actual `/health` repos list, most commonly). Mirrors the
    # branch-not-on-remote block above so this stall is operator-visible
    # instead of only a log.error() line.
    no_eligible_reviewer = [
        a for a in board.completed
        if a.type == "work" and a.review_state == "no_eligible_reviewer"
    ]
    if no_eligible_reviewer:
        click.echo("")
        click.echo("⚠ No reviewer available (review blocked — all candidates rejected):")
        for a in no_eligible_reviewer:
            click.echo(
                f"  #{a.issue_number}: {a.issue_title} ({a.repo_name})"
                f"  [no eligible reviewer]"
            )
            click.echo(
                "    Every configured machine for this repo rejected the dispatch."
                " Check that each agent's /health 'repos' list matches coordinator.yml,"
                " then re-run 'coord notify' to retry review dispatch."
            )

    # Show completed work assignments with review lifecycle state.
    _REVIEW_STATE_TAGS = {
        "pending": "[awaiting review]",
        "dispatched": "[review dispatched]",
        "done": "[review done]",
        "cap_hit": "[⚠ iteration cap hit — manual action required]",
        "branch_not_on_remote": "[⚠ branch not on remote — push required]",
        "no_eligible_reviewer": "[⚠ no reviewer available — check agent /health vs coordinator.yml]",
        # #1534: the branch carries no commits over its base, so there is
        # nothing to review. Almost always means the work assignment produced
        # nothing (e.g. a usage-limit kill) and should be re-dispatched.
        "zero_commits": "[⚠ branch has 0 commits — nothing to review, re-dispatch the work]",
    }
    work_completed = [a for a in board.completed if a.type == "work"]
    if work_completed:
        by_time = sorted(work_completed, key=lambda a: a.finished_at or 0, reverse=True)[:10]
        click.echo("")
        click.echo("Completed work assignments:")
        for a in by_time:
            # test_state="failed" takes priority: the review gate is correctly
            # held but "[awaiting review]" would mislead the operator into
            # thinking the item is queued to move forward (real incident: #1116).
            if getattr(a, "test_state", None) == "failed":
                rs_tag = "[✗ test FAILED — needs fix]"
            else:
                rs_tag = _REVIEW_STATE_TAGS.get(a.review_state or "", "")
            rs_suffix = f"  {rs_tag}" if rs_tag else ""
            click.echo(
                f"  #{a.issue_number}: {a.issue_title} ({a.repo_name}){rs_suffix}"
            )

    notified = {} if svc else load_notified()
    if notified:
        dispatched_by_id = {r["assignment_id"]: r for r in load_dispatched()}
        items = sorted(notified.items(), key=lambda kv: kv[1].get("posted_at", 0), reverse=True)[:5]
        click.echo("")
        click.echo("Recent issue comment activity:")
        for aid, info in items:
            record = dispatched_by_id.get(aid, {})
            repo = record.get("repo_github", "?")
            issue = record.get("issue_number", "?")
            click.echo(f"  [{info['event']}] {repo}#{issue} (assignment {aid})")

    # Burn-rate warning: show a one-liner when spend rate is high.
    try:
        from coord.state import load_session
        from coord.usage import build_session_usage, format_burn_rate_line
        import datetime

        sess = None if svc else load_session()
        started_at: float | None = None
        if sess and sess.get("started_at"):
            try:
                dt = datetime.datetime.fromisoformat(
                    sess["started_at"].rstrip("Z").replace("Z", "+00:00")
                )
                started_at = dt.replace(tzinfo=datetime.timezone.utc).timestamp()
            except (ValueError, AttributeError):
                pass

        all_assignments = list(board.active) + list(board.completed)
        session_usage = build_session_usage(all_assignments, started_at=started_at)
        burn_line = format_burn_rate_line(session_usage)
        if burn_line:
            click.echo("")
            click.echo(burn_line)
    except (ImportError, OSError, ValueError, KeyError):
        pass  # Never let usage tracking break the status command.


@click.command(
    help=(
        "Fleet-wide prereq report: is this machine fit to be routed work?\n\n"
        "#1570 E -- \"One command, whole fleet, prereq status per machine. "
        "Would have answered [the #1564 gh-version incident] in seconds.\" "
        "Reads each machine's already-probed tool_versions straight out of "
        "/health (#1570 B), so it costs exactly what `coord status` costs "
        "-- no SSHing around the fleet by hand.\n\n"
        "Exits 1 if any machine is unreachable, hasn't upgraded to publish "
        "tool_versions yet, fails a baseline prereq, or claims a capability "
        "its own probe can't back up."
    )
)
@_CONFIG_OPTION
@click.option("--machine", "machine_filter", default=None, help="Only check this machine.")
@click.option(
    "--timeout", default=3.0, show_default=True, type=float,
    help="Per-machine /health timeout (seconds).",
)
def doctor(config_path: Path, machine_filter: str | None, timeout: float) -> None:
    from coord.network import check_all
    from coord.prereqs import ToolProbe, unmet_capabilities

    cfg = _load_config(config_path)
    machines = cfg.machines
    if machine_filter:
        machines = [m for m in machines if m.name == machine_filter]
        if not machines:
            click.echo(
                f"error: machine {machine_filter!r} not in coordinator.yml "
                f"(have: {[m.name for m in cfg.machines]})",
                err=True,
            )
            sys.exit(2)

    statuses = check_all(machines, timeout=timeout)
    any_problem = False
    for s in statuses:
        m = s.machine
        click.echo(f"{m.name} ({m.host}):")
        if not s.is_online:
            click.echo(f"  ✗ unreachable — {s.reason}")
            any_problem = True
            continue

        health = s.health or {}
        raw_probes = health.get("tool_versions")
        if not raw_probes:
            click.echo(
                "  ⚠ no tool_versions in /health — agent predates #1570 B "
                "(`coord agent update` to see prereq status)"
            )
            any_problem = True
            continue

        probes = {
            tool: ToolProbe(
                tool=tool,
                capability=info.get("capability"),
                found=bool(info.get("found", False)),
                version=info.get("version"),
                min_version=info.get("min_version"),
                meets_floor=info.get("meets_floor"),
                what_breaks="",
            )
            for tool, info in raw_probes.items()
            if isinstance(info, dict)
        }
        for tool, p in sorted(probes.items()):
            marker = "✓" if p.ok else "✗"
            if not p.ok:
                any_problem = True
            if not p.found:
                detail = "not found"
            else:
                detail = p.version or "found (version unknown)"
            floor = f"  (>= {p.min_version} required)" if p.min_version else ""
            click.echo(f"  {marker} {tool}: {detail}{floor}")

        unmet = unmet_capabilities(m.capabilities, probes)
        for cap, reasons in unmet.items():
            any_problem = True
            for reason in reasons:
                click.echo(f"  ✗ capability {cap!r} claimed but unmet — {reason}")

    if any_problem:
        sys.exit(1)


@click.command("show-plan", help="Pretty-print the structured plan for a plan-only assignment.")
@click.argument("assignment_id")
def show_plan(assignment_id: str) -> None:
    from coord.board_service import read_board
    from coord.plan_parser import WorkerPlan, parse_plan_from_log
    from coord.state import COORD_DIR, load_plans

    board = read_board()
    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    if assignment.type != "plan":
        atype = assignment.type
        click.echo(
            f"error: assignment {assignment_id} is type {atype!r}, not 'plan'",
            err=True,
        )
        sys.exit(1)

    # 1. Try the plan cached on the board/assignment record.
    plan_dict = assignment.plan
    if plan_dict is None:
        plans = load_plans()
        plan_dict = plans.get(assignment_id)

    # 2. Fall back to parsing the log directly (works when agent is local).
    if plan_dict is None:
        local_log = COORD_DIR / "logs" / f"{assignment_id}.log"
        try:
            worker_plan = parse_plan_from_log(local_log)
        except Exception:  # noqa: BLE001
            worker_plan = None
        if worker_plan is not None:
            plan_dict = worker_plan.to_dict()

    if plan_dict is None:
        click.echo(
            f"No structured plan found for assignment {assignment_id}.\n"
            "Possible reasons: the worker has not completed yet, the log is on "
            "a remote machine, or the worker did not output plan sections.\n"
            "Run 'coord notify' after the worker finishes to parse and cache the plan."
        )
        return

    _display_plan(WorkerPlan.from_dict(plan_dict), assignment)


def _display_plan(plan: object, assignment: object) -> None:
    """Pretty-print a WorkerPlan to stdout."""
    from coord.plan_parser import WorkerPlan  # noqa: PLC0415

    assert isinstance(plan, WorkerPlan)

    repo_name = getattr(assignment, "repo_name", "?")
    issue_number = getattr(assignment, "issue_number", "?")
    issue_title = getattr(assignment, "issue_title", "")
    machine_name = getattr(assignment, "machine_name", "?")
    assignment_id = getattr(assignment, "assignment_id", "?")

    click.echo(
        f"## Plan — {repo_name} #{issue_number}: {issue_title}"
    )
    click.echo(f"Assignment: {assignment_id}  Machine: {machine_name}")

    if plan.plan:
        click.echo("")
        click.echo("### Summary")
        click.echo(plan.plan)

    if plan.files_read:
        click.echo("")
        click.echo("### Files Read")
        for f in plan.files_read:
            click.echo(f"  {f}")

    if plan.files_modify:
        click.echo("")
        click.echo("### Files to Modify")
        for f in plan.files_modify:
            click.echo(f"  {f}")

    if plan.approach:
        click.echo("")
        click.echo("### Approach")
        click.echo(plan.approach)

    if plan.risks:
        click.echo("")
        click.echo("### Risks")
        click.echo(plan.risks)

    if plan.estimate:
        click.echo("")
        click.echo("### Estimate")
        click.echo(plan.estimate)


def _diagnose_via_daemon(svc, params: dict) -> None:
    """#diagnose: run ``coord diagnose`` on the daemon host (canonical board +
    gh + ssh access to the fleet) and relay its output, so the per-stage doctor
    does real work from a thin client instead of no-opping against an empty
    local board.  Mirrors ``_reconcile_via_daemon``."""
    from coord.client import post_record  # noqa: PLC0415

    try:
        resp = post_record(svc, "/diagnose", params, timeout=180.0)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: diagnose via daemon failed: {exc}", err=True)
        sys.exit(1)
    output = resp.get("output") or ""
    if output:
        click.echo(output, nl=False)
    if resp.get("error"):
        click.echo(f"error: {resp['error']}", err=True)
    code = resp.get("exit_code") or 0
    if code:
        sys.exit(int(code))


@click.command(
    help=(
        "Diagnose and fix a specific pipeline stage of an issue.\n\n"
        "Inspects the stage (phantom 'running' rows, dropped review findings, "
        "stale-but-live sessions, merged-but-grey boxes, orphaned worktrees), "
        "makes a BEST-EFFORT non-destructive recovery (finalize, recover review "
        "findings from the session transcript, reconcile merges), and ALWAYS "
        "reconciles this issue's board rows. When recovery isn't possible it "
        "reports needs_reset=true; re-run with --reset to clear the stage's "
        "rows/claim/worktree and stop a live session — KEEPING the branch + "
        "commits, so the stage is re-dispatchable.\n\n"
        "Pass --orphan-worktrees instead of REPO/ISSUE to run a local fleet sweep "
        "that removes coordinator worktrees with no live tmux session and no "
        "uncommitted work.  Dirty worktrees are reported but never auto-deleted."
    )
)


@click.argument("repo", required=False, default=None)
@click.argument("issue", type=int, required=False, default=None)
@click.option(
    "--stage",
    type=click.Choice(["plan", "work", "review", "test", "merge"]),
    default=None,
    help="Which stage to diagnose (default: the issue's most-recent stage).",
)


@click.option(
    "--reset",
    is_flag=True,
    help="Non-destructive reset: clear the stage's rows/claim/worktree and stop "
    "a live session, KEEPING the branch + commits (stage re-dispatchable).",
)


@click.option("--dry-run", is_flag=True, help="Report findings without writing.")
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help=(
        "#935: emit the DiagnoseResult as a JSON object on stdout (in addition to "
        "the human-readable lines and the DIAGNOSE_RESULT trailer).  The JSON block "
        "is printed BEFORE the trailer so callers can parse it without grepping."
    ),
)
@click.option(
    "--orphan-worktrees",
    is_flag=True,
    help=(
        "#618: local fleet sweep — find and remove coordinator worktrees "
        "(~/.coord/worktrees/*) whose assignment has no live tmux session and "
        "no uncommitted work.  Dirty worktrees are reported but never deleted."
    ),
)


@_CONFIG_OPTION
def diagnose(
    repo: str | None,
    issue: int | None,
    stage: str | None,
    reset: bool,
    dry_run: bool,
    config_path: Path,
    output_json: bool = False,
    orphan_worktrees: bool = False,
) -> None:
    """Per-stage doctor — diagnose, best-effort recover, optional reset."""
    # ── #618: --orphan-worktrees fleet sweep ─────────────────────────────────
    if orphan_worktrees:
        _diagnose_orphan_worktrees(config_path, dry_run=dry_run)
        return

    if repo is None or issue is None:
        click.echo(
            "error: REPO and ISSUE are required (or pass --orphan-worktrees for a fleet sweep).",
            err=True,
        )
        sys.exit(2)

    # #584: the canonical board + gh + fleet ssh live on the daemon host, so on
    # a thin client this must run there (an empty local board would no-op).
    # COORD_DIAGNOSE_ON_DAEMON guards the daemon against re-routing to itself.
    from coord.board_service import daemon_reroute_target  # noqa: PLC0415

    _svc = daemon_reroute_target("COORD_DIAGNOSE_ON_DAEMON")
    if _svc is not None:
        _diagnose_via_daemon(
            _svc,
            {
                "repo": repo,
                "issue": issue,
                "stage": stage,
                "reset": reset,
                "dry_run": dry_run,
                "output_json": output_json,
            },
        )
        return

    from coord.diagnose import current_stage, diagnose_stage  # noqa: PLC0415
    from coord.state import build_board  # noqa: PLC0415

    cfg = _load_config(config_path)
    board = build_board()
    resolved_stage = stage or current_stage(board, repo, issue)
    res = diagnose_stage(
        board, cfg, repo, issue, resolved_stage, reset=reset, dry_run=dry_run
    )
    # NOTE: deliberately NO save_board here.  Every diagnose write goes through
    # the authoritative seam (finalize→post_completion, recover→post_result,
    # reconcile→state.update_*), which writes the canonical DB directly.  A
    # save_board would persist the STALE in-memory snapshot (built before those
    # seam writes) and clobber them — e.g. flip a just-finalized phantom back to
    # 'running' (caught live on quadraui #366).

    click.echo(f"diagnose {repo} #{issue} — stage={resolved_stage}"
               + (" [reset]" if reset else "") + (" [dry-run]" if dry_run else ""))
    for f in res.findings:
        click.echo(f"  · {f}")
    for a in res.actions_taken:
        click.echo(f"  ✓ {a}")
    if res.needs_reset and not reset:
        click.echo("  ⚠ still wedged — re-run with --reset to clear the stage "
                   "(keeps the branch + commits).")
    # #935 Part C: emit JSON dict before the trailer when --json is requested.
    # The daemon handler also passes output_json through so remote calls relay it.
    if output_json:
        import json  # noqa: PLC0415
        click.echo("DIAGNOSE_JSON:" + json.dumps(res.to_json_dict()))
    click.echo(res.summary_line())


def _diagnose_orphan_worktrees(config_path: Path, *, dry_run: bool) -> None:
    """#618: local fleet sweep — find and prune orphaned coordinator worktrees.

    An orphaned worktree is one under ``~/.coord/worktrees/`` whose
    assignment_id has no live tmux session and no running/pending DB row.
    Dirty worktrees (uncommitted changes) are reported but never deleted.

    #1445: also runs :func:`coord.agent.check_worktree_writable` against
    ``~/.coord/worktrees/`` first and reports a DEGRADED line naming the
    path and (when it's a permission rule at fault) the exact rule/file —
    this machine invariant is otherwise invisible until a dispatched worker
    burns a full session discovering it can't save anything. This check is
    LOCAL-machine only, same as the rest of this sweep; it does not reach
    out to other machines in the fleet.

    #1445 review: ``--dry-run`` means "report findings without writing," so
    the OS-level half of the writability probe — which ``mkdir(parents=True,
    exist_ok=True)``s ``worktrees_dir`` (a real, persistent creation on a
    machine that never had one) and does a write+unlink of a probe file — is
    skipped under ``--dry-run``. The deny-rule scan
    (:func:`coord.agent.find_blocking_deny_rule`) is read-only and still runs.
    On a machine that has never had a worktree, ``--dry-run`` therefore
    reports "does not exist yet" rather than creating it just to say it's
    empty.
    """
    from coord.diagnose import (  # noqa: PLC0415
        _find_orphaned_worktrees,
        _prune_orphaned_worktrees,
    )
    from coord.interactive import (  # noqa: PLC0415
        tmux_available,
        tmux_session_name,
        tmux_session_alive,
    )
    from coord.agent import check_worktree_writable, find_blocking_deny_rule  # noqa: PLC0415
    from coord.board_service import read_board  # noqa: PLC0415
    from coord.state import COORD_DIR  # noqa: PLC0415

    cfg = _load_config(config_path)
    board = read_board()
    worktrees_dir = COORD_DIR / "worktrees"

    # #1445: surface a machine that can't write its own worktrees as
    # DEGRADED rather than silently idle-and-ready — this is the same
    # fleet invariant `AgentServer.assign()` now preflights before every
    # dispatch, checked here so an operator (or `scripts/drive-issue.sh`)
    # can catch it ahead of time with a plain `coord diagnose --orphan-worktrees`.
    if dry_run:
        if not worktrees_dir.exists():
            click.echo(
                f"~/.coord/worktrees/ does not exist yet — nothing to sweep "
                f"(dry-run: skipping writability probe to avoid creating it)."
            )
            return
        blocked_by = find_blocking_deny_rule(worktrees_dir)
        if blocked_by is not None:
            click.echo(
                f"⚠ DEGRADED: workers on this machine cannot write to "
                f"{worktrees_dir}: a Claude Code permission rule denies "
                f"Edit/Write under {worktrees_dir}: {blocked_by}"
            )
        else:
            click.echo(
                f"✓ {worktrees_dir} is writable by workers "
                f"(dry-run: OS-level write probe skipped)."
            )
    else:
        write_issue = check_worktree_writable(worktrees_dir)
        if write_issue is not None:
            click.echo(f"⚠ DEGRADED: workers on this machine cannot write to {worktrees_dir}: {write_issue}")
        else:
            click.echo(f"✓ {worktrees_dir} is writable by workers.")

    # Outside --dry-run, check_worktree_writable() just mkdir'd worktrees_dir
    # (parents=True, exist_ok=True) as part of its probe, so it always exists
    # by this point — an empty directory just means no worktrees are
    # currently checked out. Under --dry-run we already returned above when
    # it didn't exist, so it's safe to iterate here too.
    if not any(worktrees_dir.iterdir()):
        click.echo("~/.coord/worktrees/ has no worktrees — nothing to sweep.")
        return

    # Collect all assignment_ids with live tmux sessions.
    tmux_ok = tmux_available()
    live_tmux: set[str] = set()
    if tmux_ok:
        for entry in worktrees_dir.iterdir():
            if not entry.is_dir():
                continue
            aid = entry.name
            if tmux_session_alive(tmux_session_name(aid)):
                live_tmux.add(aid)

    # All running/pending assignment_ids from the board (includes live tmux ones
    # from the board's active set; combine with live_tmux for sessions whose DB
    # rows may already be gone).
    running_ids: set[str] = {
        a.assignment_id
        for a in board.active
        if a.assignment_id
    }
    active_ids = running_ids | live_tmux

    total_removed: list[Path] = []
    total_skipped: list[Path] = []

    for repo in cfg.repos:
        # Find any local checkout for this repo.
        repo_path: Path | None = None
        for machine in cfg.machines:
            rp = machine.repo_path(repo.name)
            if rp:
                candidate = Path(rp).expanduser()
                if candidate.exists():
                    repo_path = candidate
                    break
        if repo_path is None:
            continue

        # Delegate porcelain parsing to the shared helper (branch=None → any branch).
        orphans = _find_orphaned_worktrees(
            repo_path, None, active_assignment_ids=active_ids, worktrees_dir=worktrees_dir
        )
        if not orphans:
            continue

        click.echo(f"{repo.name}: found {len(orphans)} orphaned worktree(s)")
        for wt in orphans:
            click.echo(f"  {wt}")
        if dry_run:
            click.echo(f"  (dry-run) would prune {len(orphans)} worktree(s)")
            total_skipped.extend(orphans)
            continue

        removed, skipped = _prune_orphaned_worktrees(repo_path, orphans)
        for wt in removed:
            click.echo(f"  ✓ removed {wt}")
        for wt in skipped:
            click.echo(f"  ⚠ skipped (uncommitted work) {wt}")
        total_removed.extend(removed)
        total_skipped.extend(skipped)

    click.echo(
        f"orphan-worktrees sweep: {len(total_removed)} removed"
        + (f", {len(total_skipped)} skipped (dirty — inspect manually)" if total_skipped else "")
        + (" [dry-run]" if dry_run else "")
    )


@click.command(help="Show per-assignment and per-model cost breakdown with burn rate.")
@_CONFIG_OPTION
@click.option(
    "--remote",
    is_flag=True,
    help="Fetch cost data from agent servers for assignments without local logs.",
)


@click.option(
    "--timeout",
    default=3.0,
    show_default=True,
    type=float,
    help="Per-machine HTTP timeout for --remote lookups (seconds).",
)
@click.option(
    "--today",
    is_flag=True,
    help="Limit the view to the local calendar day (#1115).",
)
@click.option(
    "--week",
    is_flag=True,
    help="Limit the view to the current ISO week, Monday 00:00 -> next Monday (#1119).",
)
@click.option(
    "--month",
    is_flag=True,
    help="Limit the view to the current calendar month (#1119).",
)
@click.option(
    "--since",
    "since_spec",
    default=None,
    help="Limit the view to legs since <ISO date | Nd | Nh> (#1115).",
)
@click.option(
    "--by-issue",
    "by_issue",
    is_flag=True,
    help="Group daemon-board usage by GitHub issue for the time window, sorted desc (#1115).",
)
@click.option(
    "--issue",
    "issue_number",
    type=int,
    default=None,
    help="Per-stage drill-down for one issue number — all legs, oldest-first (#1115).",
)
@click.option(
    "--by",
    "by_dim",
    type=click.Choice(["repo", "week", "month", "issue"]),
    default=None,
    help="Cross-cut daemon-board usage by dimension: repo (cross-repo rollup), "
    "week/month (time-bucketed spend series), or issue (same as --by-issue) (#1119).",
)
@click.option(
    "--by-time",
    "by_time",
    is_flag=True,
    help="Time-spent view: rank wall-clock by stage-type, or by issue when combined "
    "with --by issue (#1119).",
)
@click.option(
    "--sort",
    "sort_by",
    type=click.Choice(["cost", "tokens", "time"]),
    default=None,
    help="Sort order (always descending). Default: cost for rollup views, time for --by-time.",
)
@click.option(
    "--limits",
    "show_limits",
    is_flag=True,
    help="Show the account's Max-plan 5h/weekly usage-window probe (#1466) "
    "instead of the cost breakdown — server-side/account-wide, ~60s cached.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="With --limits, emit the probe as structured JSON instead of text.",
)
def usage(
    config_path: Path,
    remote: bool,
    timeout: float,
    today: bool,
    week: bool,
    month: bool,
    since_spec: str | None,
    by_issue: bool,
    issue_number: int | None,
    by_dim: str | None,
    by_time: bool,
    sort_by: str | None,
    show_limits: bool,
    as_json: bool,
) -> None:
    if as_json and not show_limits:
        raise click.BadParameter(
            "--json only applies to --limits", param_hint="'--json'"
        )
    if show_limits:
        import json as _json

        from coord.usage_limits import format_plan_limits, get_plan_limits

        limits = get_plan_limits()
        if as_json:
            click.echo(_json.dumps(limits.to_dict(), indent=2))
        else:
            click.echo(format_plan_limits(limits))
        return
    if issue_number is not None:
        _usage_issue_drill(config_path, issue_number, today=today, week=week, month=month, since_spec=since_spec)
        return
    if by_time:
        if by_dim in ("repo", "week", "month"):
            raise click.BadParameter(
                f"--by-time only supports --by issue (or no --by); got --by {by_dim}",
                param_hint="'--by'",
            )
        _usage_by_time(
            config_path,
            today=today, week=week, month=month, since_spec=since_spec,
            by_dim=by_dim, sort_by=sort_by,
        )
        return
    if by_issue or by_dim == "issue":
        _usage_by_issue(
            config_path,
            today=today, week=week, month=month, since_spec=since_spec,
            sort_by=_usage_resolve_sort(sort_by, default="cost"),
        )
        return
    if by_dim in ("repo", "week", "month"):
        _usage_by_dim(
            config_path, by_dim,
            today=today, week=week, month=month, since_spec=since_spec,
            sort_by=_usage_resolve_sort(sort_by, default="cost"),
        )
        return

    from coord.board_service import read_board
    from coord.state import load_session
    from coord.usage import build_session_usage, filter_assignments_in_window, format_usage_report

    board = read_board()
    all_assignments = list(board.active) + list(board.completed)

    # Resolve + apply the window flags to the legacy (no --by/--by-time/
    # --by-issue/--issue) view too (#1119 review finding #1) — previously
    # --today/--week/--month/--since were silently ignored here, with no
    # validation (even --week --month together fell through to this branch
    # unchecked). Calling _usage_resolve_window unconditionally means the
    # existing n_set > 1 guard now fires for the bare case as well. Only set
    # window_label when a flag was actually given, so the default
    # (unwindowed) report stays byte-for-byte unchanged.
    window_label: str | None = None
    if today or week or month or since_spec:
        window = _usage_resolve_window(today, week, month, since_spec)
        window_label = window.label
        all_assignments = filter_assignments_in_window(all_assignments, window)

    # Resolve session start time from session.json
    started_at: float | None = None
    sess = load_session()
    if sess and sess.get("started_at"):
        import datetime
        try:
            dt = datetime.datetime.fromisoformat(
                sess["started_at"].rstrip("Z").replace("Z", "+00:00")
            )
            started_at = dt.replace(tzinfo=datetime.timezone.utc).timestamp()
        except (ValueError, AttributeError):
            pass

    # Optionally fetch remote cost data for assignments without local logs.
    remote_by_id: dict[str, dict] = {}
    if remote and all_assignments:
        cfg = _load_config(config_path)
        from coord.network import fetch_status

        # Build a map from machine_name → assignments on that machine.
        by_machine: dict[str, list] = {}
        for a in all_assignments:
            if a.assignment_id:
                by_machine.setdefault(a.machine_name, []).append(a)

        for machine in cfg.machines:
            if machine.name not in by_machine:
                continue
            try:
                data = fetch_status(machine, timeout=timeout)
            except Exception:
                continue
            if not data:
                continue
            for entry in (data.get("active") or []) + (data.get("completed") or []):
                aid = entry.get("id") or entry.get("assignment_id")
                if aid:
                    remote_by_id[aid] = entry

    session = build_session_usage(
        all_assignments,
        remote_by_id=remote_by_id if remote_by_id else None,
        started_at=started_at,
    )
    click.echo(format_usage_report(session, window_label=window_label))


_SINCE_WEEKS_RE = re.compile(r"^(\d+)\s*w$", re.IGNORECASE)


def _normalize_since_spec(spec: str) -> str:
    """Expand a ``Nw`` (weeks) shorthand to ``(N*7)d`` before handing it to
    Core's ``Window.since`` (#1119 requirement 1 / acceptance example
    ``--since 8w``). Core's ``since`` regex only knows ``Nd``/``Nh`` (see
    ``coord.usage_rollup._SINCE_RELATIVE_RE``) — this is a thin CLI-side
    syntactic convenience, not new window-resolution logic, so it stays here
    rather than in Core. Anything else (ISO date, ``Nd``, ``Nh``) passes
    through unchanged for Core to validate.
    """
    match = _SINCE_WEEKS_RE.match(spec.strip())
    if match:
        return f"{int(match.group(1)) * 7}d"
    return spec


def _usage_resolve_window(today: bool, week: bool, month: bool, since_spec: str | None):
    """Resolve the ``--today``/``--week``/``--month``/``--since`` flags to a
    :class:`coord.usage_rollup.TimeWindow` for the daemon-sourced rollup views
    (#1115/#1119). At most one of the four may be given. None given falls back
    to the current session's start time (open-ended); no session at all falls
    back to an unbounded window.

    ``--week``/``--month`` are resolved via :mod:`coord.usage_rollup`'s own
    ``window_week``/``window_month`` presets — called, not reimplemented,
    per #1119's "consumes Core, no new aggregation logic" scope.
    """
    n_set = sum([bool(today), bool(week), bool(month), bool(since_spec)])
    if n_set > 1:
        raise click.BadParameter(
            "pass at most one of --today, --week, --month, --since",
            param_hint="'--today'/'--week'/'--month'/'--since'",
        )

    from coord.usage_rollup import Window, window_month, window_week

    if today:
        return Window.today()
    if week:
        return window_week()
    if month:
        return window_month()
    if since_spec:
        try:
            window = Window.since(_normalize_since_spec(since_spec))
        except ValueError as e:
            raise click.BadParameter(str(e), param_hint="'--since'") from e
        # Preserve the human-readable spec the user actually typed in the
        # printed label (e.g. "since 8w"), even though it was expanded to
        # "56d" for Core's date math above.
        from dataclasses import replace

        return replace(window, label=f"since {since_spec}")

    from coord.state import load_session

    sess = load_session()
    if sess and sess.get("started_at"):
        import datetime
        try:
            dt = datetime.datetime.fromisoformat(
                sess["started_at"].rstrip("Z").replace("Z", "+00:00")
            )
            started_at = dt.replace(tzinfo=datetime.timezone.utc).timestamp()
            return Window(start=started_at, end=None, label="session")
        except (ValueError, AttributeError):
            pass
    return Window(start=None, end=None, label="all")


def _usage_resolve_sort(sort_by: str | None, *, default: str) -> str:
    """Resolve the ``--sort`` flag: explicit value wins, else *default*
    (#1119 — different views default to a different natural ranking key)."""
    return sort_by or default


def _usage_sort_key(sort_by: str):
    """Key function for sorting an :func:`~coord.usage_rollup.aggregate`
    ``groups`` list by *sort_by* (``cost``/``tokens``/``time``), descending."""
    if sort_by == "tokens":
        def _total_tokens(group: dict) -> int:
            t = group["tokens"]
            return t["input"] + t["output"] + t["cache_read"] + t["cache_creation"]
        return _total_tokens
    if sort_by == "time":
        return lambda group: group["duration_secs"]
    return lambda group: group["cost_total"]


def _usage_by_issue(
    config_path: Path, *, today: bool, week: bool, month: bool, since_spec: str | None, sort_by: str
) -> None:
    """``coord usage --by-issue`` (contract Mock 1, #1115) — daemon-board-
    sourced per-issue cost/token rollup for the resolved time window."""
    from coord.usage import fetch_usage_rows, format_usage_by_issue, pricing_dict_from_config
    from coord.usage_rollup import aggregate

    cfg = _load_config(config_path)
    window = _usage_resolve_window(today, week, month, since_spec)
    rows = fetch_usage_rows()
    pricing = pricing_dict_from_config(cfg.pricing)
    result = aggregate(rows, by="issue", window=window, pricing=pricing)
    result["groups"].sort(key=_usage_sort_key(sort_by), reverse=True)

    click.echo(format_usage_by_issue(result, window.label))


def _usage_issue_drill(
    config_path: Path, issue_number: int, *, today: bool, week: bool, month: bool, since_spec: str | None
) -> None:
    """``coord usage --issue N`` (contract Mock 2, #1115) — per-stage drill
    for one issue's legs. Unbounded (all history) unless --today/--week/
    --month/--since is also given."""
    from coord.usage import fetch_usage_rows, format_usage_issue_drill
    from coord.usage_rollup import leg_in_window

    cfg = _load_config(config_path)
    has_window_flag = today or week or month or since_spec
    window = _usage_resolve_window(today, week, month, since_spec) if has_window_flag else None
    rows = [
        row
        for row in fetch_usage_rows()
        if int(row.get("issue_number") or 0) == issue_number
        and (window is None or leg_in_window(row, window))
    ]
    click.echo(format_usage_issue_drill(rows, issue_number, cfg.pricing))


def _usage_by_dim(
    config_path: Path,
    by_dim: str,
    *,
    today: bool,
    week: bool,
    month: bool,
    since_spec: str | None,
    sort_by: str,
) -> None:
    """``coord usage --by repo|week|month`` (#1119) — cross-cut daemon-board
    usage by *by_dim* for the resolved time window. ``repo`` is the
    cross-repo rollup (contract Mock 3); ``week``/``month`` are a
    time-bucketed spend series over a wider window (e.g. ``--since 8w --by
    week``)."""
    from coord.usage import fetch_usage_rows, format_usage_by_group, pricing_dict_from_config
    from coord.usage_rollup import aggregate

    cfg = _load_config(config_path)
    window = _usage_resolve_window(today, week, month, since_spec)
    rows = fetch_usage_rows()
    pricing = pricing_dict_from_config(cfg.pricing)
    result = aggregate(rows, by=by_dim, window=window, pricing=pricing)
    result["groups"].sort(key=_usage_sort_key(sort_by), reverse=True)

    click.echo(format_usage_by_group(result, window.label, by_dim))


def _usage_by_time(
    config_path: Path,
    *,
    today: bool,
    week: bool,
    month: bool,
    since_spec: str | None,
    by_dim: str | None,
    sort_by: str | None,
) -> None:
    """``coord usage --by-time`` (contract Mock 4, #1119) — ranks where
    wall-clock is going: by stage-type (default) or, combined with
    ``--by issue``, by issue. Defaults to ranking by ``time`` (that's the
    point of the view) unless ``--sort`` explicitly overrides."""
    from coord.usage import fetch_usage_rows, format_usage_by_time, pricing_dict_from_config
    from coord.usage_rollup import aggregate

    dim = "issue" if by_dim == "issue" else "stage"
    resolved_sort = _usage_resolve_sort(sort_by, default="time")

    cfg = _load_config(config_path)
    window = _usage_resolve_window(today, week, month, since_spec)
    rows = fetch_usage_rows()
    pricing = pricing_dict_from_config(cfg.pricing)
    result = aggregate(rows, by=dim, window=window, pricing=pricing)
    result["groups"].sort(key=_usage_sort_key(resolved_sort), reverse=True)

    click.echo(format_usage_by_time(result, window.label, dim))
