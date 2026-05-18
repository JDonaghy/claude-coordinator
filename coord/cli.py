"""Click CLI entry point for the `coord` command."""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import click
import httpx

from coord import __version__
from coord.config import Config, ConfigError, DEFAULT_CONFIG_PATH, load
from coord.brain import AGENT_PORT

AGENT_PORT = 7433


_CONFIG_OPTION = click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=DEFAULT_CONFIG_PATH,
    show_default=True,
    help="Path to coordinator.yml.",
)


def _load_config(path: Path) -> Config:
    try:
        return load(path)
    except ConfigError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(2)


@click.group(help="Multi-agent coordinator for Claude Code workers.")
@click.version_option(__version__, prog_name="coord")
def main() -> None:
    """coord — coordinate Claude Code workers across machines and repos."""


@main.command(help="Print the coord version.")
def version() -> None:
    click.echo(f"coord {__version__}")


@main.command("config", help="Load coordinator.yml and pretty-print the parsed config.")
@_CONFIG_OPTION
def config_cmd(config_path: Path) -> None:
    cfg = _load_config(config_path)
    click.echo(f"# {cfg.path}")
    click.echo("")
    click.echo("Repos:")
    for r in cfg.repos:
        deps = f"  depends_on: {', '.join(r.depends_on)}" if r.depends_on else "  depends_on: (none)"
        click.echo(f"  - {r.name} ({r.github}) [branch: {r.default_branch}]")
        click.echo(f"  {deps}")
    click.echo("")
    click.echo("Machines:")
    for m in cfg.machines:
        caps = ", ".join(m.capabilities) if m.capabilities else "(none)"
        repos = ", ".join(m.repos) if m.repos else "(none)"
        click.echo(f"  - {m.name} @ {m.host}")
        click.echo(f"    capabilities: {caps}")
        click.echo(f"    repos: {repos}")


def _not_implemented(name: str) -> None:
    click.echo(f"coord {name}: not implemented yet (stub)", err=True)
    sys.exit(1)


@main.command(help="Interactive setup; generates coordinator.yml.")
def init() -> None:
    _not_implemented("init")


@main.command(help="Start the agent server on this machine (port 7433).")
@_CONFIG_OPTION
@click.option(
    "--machine",
    "machine_name",
    default=None,
    help="Machine name from coordinator.yml (defaults to hostname match).",
)
@click.option("--host", "bind_host", default="0.0.0.0", show_default=True)
@click.option("--port", "bind_port", default=AGENT_PORT, show_default=True, type=int)
def agent(config_path: Path, machine_name: str | None, bind_host: str, bind_port: int) -> None:
    import uvicorn

    from coord.agent import AgentServer
    from coord.agent_app import build_app

    cfg = _load_config(config_path)
    machine = _resolve_machine(cfg, machine_name)

    server = AgentServer(
        machine_name=machine.name,
        capabilities=machine.capabilities,
        repos=machine.repos,
    )
    app = build_app(server)
    click.echo(
        f"coord agent: machine={machine.name} repos={machine.repos} "
        f"listening on http://{bind_host}:{bind_port}"
    )
    try:
        uvicorn.run(app, host=bind_host, port=bind_port, log_level="info")
    finally:
        server.shutdown()


def _resolve_machine(cfg: Config, explicit_name: str | None):
    if explicit_name:
        m = next((m for m in cfg.machines if m.name == explicit_name), None)
        if m is None:
            click.echo(
                f"error: machine {explicit_name!r} not in coordinator.yml "
                f"(have: {[m.name for m in cfg.machines]})",
                err=True,
            )
            sys.exit(2)
        return m

    hostname = socket.gethostname()
    short = hostname.split(".")[0]
    candidates = [m for m in cfg.machines if m.name == short or m.host == hostname or m.host.split(".")[0] == short]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        click.echo(
            f"error: could not match hostname {hostname!r} to any machine in coordinator.yml. "
            f"Pass --machine explicitly. Known: {[m.name for m in cfg.machines]}",
            err=True,
        )
        sys.exit(2)
    click.echo(
        f"error: hostname {hostname!r} matches multiple machines: "
        f"{[m.name for m in candidates]}. Pass --machine explicitly.",
        err=True,
    )
    sys.exit(2)


@main.command(help="Show all machines, assignments, and connectivity.")
@_CONFIG_OPTION
@click.option("--machine", "machine_filter", default=None, help="Only show this machine.")
@click.option("--timeout", default=3.0, show_default=True, type=float, help="Per-machine health-check timeout (seconds).")
def status(config_path: Path, machine_filter: str | None, timeout: float) -> None:
    from coord.deps import blocked_repos as compute_blocked, build_dep_graph
    from coord.network import check_all, fetch_status
    from coord.state import build_board, load_dispatched, load_notified

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

    statuses = check_all(machines, timeout=timeout)
    click.echo("Machines:")
    for s in statuses:
        m = s.machine
        latency = f" ({s.latency_ms:.0f}ms)" if s.latency_ms is not None else ""
        if s.is_online:
            assignments = fetch_status(m, timeout=timeout)
            active = (assignments or {}).get("active", [])
            if active:
                a = active[0]
                spec = a.get("spec", {})
                detail = f"busy — #{spec.get('issue_number', '?')}: {spec.get('issue_title', '?')}"
            else:
                detail = "idle"
            label = f"{s.state} • {detail}{latency}"
        else:
            assignments = None
            label = f"{s.state} — {s.reason}{latency}"
        repos = ", ".join(m.repos) if m.repos else "(none)"
        click.echo(f"  {m.name:15s} [{label}]")
        click.echo(f"    host: {m.host}  repos: {repos}")

        if assignments:
            for entry in assignments.get("active", []):
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

    # Blocked repos
    board = build_board()
    blocked = compute_blocked(cfg.repos, board.active)
    if blocked:
        click.echo("")
        click.echo("Blocked repos:")
        for repo_name, reasons in blocked.items():
            click.echo(f"  {repo_name}:")
            for reason in reasons:
                click.echo(f"    - {reason}")

    notified = load_notified()
    if not notified:
        return

    dispatched_by_id = {r["assignment_id"]: r for r in load_dispatched()}
    items = sorted(notified.items(), key=lambda kv: kv[1].get("posted_at", 0), reverse=True)[:5]
    click.echo("")
    click.echo("Recent issue comment activity:")
    for aid, info in items:
        record = dispatched_by_id.get(aid, {})
        repo = record.get("repo_github", "?")
        issue = record.get("issue_number", "?")
        click.echo(f"  [{info['event']}] {repo}#{issue} (assignment {aid})")


@main.command(help="Brain proposes assignments for idle machines.")
@_CONFIG_OPTION
@click.option("--dry-run", is_flag=True, help="Plan without saving proposals.")
def plan(config_path: Path, dry_run: bool) -> None:
    from coord.brain import propose
    from coord.state import save_proposals, save_split_proposals

    cfg = _load_config(config_path)
    click.echo("Gathering context and calling Claude...\n")

    try:
        proposals, splits = propose(cfg)
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


@main.command(help="Dispatch approved assignments (comma-separated IDs).")
@click.argument("ids")
@_CONFIG_OPTION
@click.option("--dry-run", is_flag=True, help="Show what would be dispatched.")
def approve(ids: str, config_path: Path, dry_run: bool) -> None:
    from coord.deps import blocked_repos as compute_blocked
    from coord.dispatch import compute_do_not_touch, dispatch, post_briefing
    from coord.state import (
        build_board,
        clear_proposals,
        load_dispatched,
        load_proposals,
        record_dispatched,
        save_board,
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
    board = build_board()
    blocked = compute_blocked(cfg.repos, board.active)
    for p in selected:
        if p.repo_name in blocked:
            click.echo(f"  warning: {p.repo_name} is blocked by upstream work:", err=True)
            for reason in blocked[p.repo_name]:
                click.echo(f"    - {reason}", err=True)

    in_flight = load_dispatched()

    from coord.network import classify_error

    for p in selected:
        click.echo(f"[{p.id}] {p.machine_name} → {p.repo_name} #{p.issue_number}: {p.issue_title}")
        if dry_run:
            click.echo("     (dry run — not dispatched)")
            continue
        try:
            response = dispatch(p, cfg)
        except httpx.HTTPError as e:
            state, reason = classify_error(e)
            click.echo(
                f"     dispatch failed: {p.machine_name} {state} — {reason}",
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
            from coord.state import record_dispatched as _record
            _record(assignment_id=assignment_id, proposal=p, repo_github=repo.github)

        try:
            do_not_touch = compute_do_not_touch(p, peers=selected, in_flight=in_flight)
            post_briefing(p, cfg, assignment_id=assignment_id, do_not_touch=do_not_touch)
            click.echo("     briefing posted to GitHub")
        except Exception as e:
            click.echo(f"     briefing post failed: {e}", err=True)

    if not dry_run:
        clear_proposals()
        board = build_board()
        board.round_number += 1
        save_board(board)
        click.echo("\nPending proposals cleared. Board saved.")


@main.command(help="View claude -p output for a specific assignment.")
@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option("--follow", "-f", is_flag=True, help="Follow output (like tail -f).")
@click.option(
    "--machine",
    "machine_filter",
    default=None,
    help="Fetch from this machine over the network (otherwise auto-resolved).",
)
@click.option("--local", "force_local", is_flag=True, help="Read from local ~/.coord/logs only.")
def log(
    assignment_id: str,
    config_path: Path,
    follow: bool,
    machine_filter: str | None,
    force_local: bool,
) -> None:
    from coord.state import load_dispatched

    target_machine = None
    if not force_local:
        if machine_filter:
            cfg_loaded = _load_config(config_path)
            target_machine = next(
                (m for m in cfg_loaded.machines if m.name == machine_filter), None
            )
            if target_machine is None:
                click.echo(
                    f"error: machine {machine_filter!r} not in coordinator.yml",
                    err=True,
                )
                sys.exit(2)
        else:
            record = next(
                (r for r in load_dispatched() if r.get("assignment_id") == assignment_id),
                None,
            )
            if record is not None:
                cfg_loaded = _load_config(config_path)
                target_machine = next(
                    (m for m in cfg_loaded.machines if m.name == record["machine_name"]),
                    None,
                )

    if target_machine is None:
        _log_local(assignment_id, follow)
        return

    _log_remote(target_machine, assignment_id, follow)


def _log_local(assignment_id: str, follow: bool) -> None:
    from coord.agent import DEFAULT_STATE_DIR
    import time as _time

    log_path = DEFAULT_STATE_DIR / "logs" / f"{assignment_id}.log"
    if not log_path.exists():
        click.echo(f"error: no log found for assignment {assignment_id!r}", err=True)
        click.echo(f"  looked in: {log_path}", err=True)
        click.echo(
            "  hint: pass --machine NAME to fetch a remote log, or check `coord status`",
            err=True,
        )
        sys.exit(1)

    if follow:
        with open(log_path) as f:
            while True:
                line = f.readline()
                if line:
                    click.echo(line, nl=False)
                else:
                    _time.sleep(0.3)
    else:
        click.echo(log_path.read_text(), nl=False)


def _log_remote(machine, assignment_id: str, follow: bool) -> None:
    from coord.network import fetch_log
    import time as _time

    since = 0
    status_code, body = fetch_log(machine, assignment_id, since=since)
    if status_code == 404:
        click.echo(
            f"error: no log for assignment {assignment_id!r} on machine {machine.name!r}",
            err=True,
        )
        sys.exit(1)
    if status_code != 200:
        click.echo(
            f"error: fetching log from {machine.name} returned HTTP {status_code}",
            err=True,
        )
        sys.exit(1)
    click.echo(body.decode("utf-8", errors="replace"), nl=False)
    since = len(body)

    if not follow:
        return

    while True:
        _time.sleep(0.5)
        try:
            status_code, body = fetch_log(machine, assignment_id, since=since)
        except Exception as e:  # noqa: BLE001 — surface network errors
            click.echo(f"\n(stream interrupted: {e})", err=True)
            return
        if status_code != 200:
            click.echo(f"\n(stream interrupted: HTTP {status_code})", err=True)
            return
        if body:
            click.echo(body.decode("utf-8", errors="replace"), nl=False)
            since += len(body)


@main.command(help="Cancel a running assignment.")
@click.argument("assignment_id")
@_CONFIG_OPTION
def stop(assignment_id: str, config_path: Path) -> None:
    from coord.state import build_board, load_board, save_board

    cfg = _load_config(config_path)
    board = load_board() or build_board()

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
    save_board(board)
    click.echo(f"Board updated: {assignment.repo_name} #{assignment.issue_number} marked failed")


@main.command(help="Poll agents and post completion/failure comments on GitHub.")
@_CONFIG_OPTION
def notify(config_path: Path) -> None:
    from coord.hooks import is_round_complete, run_hooks
    from coord.notify import run as run_notify
    from coord.state import build_board, save_board

    cfg = _load_config(config_path)
    posted = run_notify(cfg)
    if not posted:
        click.echo("No new transitions to notify.")
        return
    click.echo(f"Posted {len(posted)} comment(s):")
    for t in posted:
        click.echo(
            f"  [{t.event}] {t.machine_name} → {t.repo_name} "
            f"#{t.issue_number} (assignment {t.assignment_id}, exit {t.exit_code})"
        )
    board = build_board()

    if is_round_complete(board) and cfg.hooks.on_round_complete:
        click.echo("\nRound complete — running hooks:")
        for result in run_hooks("on_round_complete", cfg, board):
            status = "ok" if result.ok else "FAILED"
            click.echo(f"  [{status}] {result.hook}: {result.message}")

    save_board(board)


@main.command(help="Recover board state after a crash or restart.")
@_CONFIG_OPTION
def resume(config_path: Path) -> None:
    from coord.reconcile import reconcile
    from coord.state import build_board, load_board, save_board

    cfg = _load_config(config_path)
    board = load_board()
    if board is None:
        click.echo("No saved board found. Rebuilding from dispatched ledger...")
        board = build_board()

    click.echo(f"Board round: {board.round_number}")
    click.echo(f"  active:    {len(board.active)} assignment(s)")
    click.echo(f"  completed: {len(board.completed)} assignment(s)")

    if board.active:
        click.echo("\nReconciling with agent servers...")
        changed = reconcile(board, cfg)
        if changed:
            click.echo(f"  {len(changed)} assignment(s) finished since last check:")
            for aid in changed:
                a = board.find_by_id(aid)
                if a:
                    click.echo(f"    {a.machine_name} → {a.repo_name} #{a.issue_number}: [{a.status}]")
        else:
            click.echo("  all active assignments still running")

    removed = board.gc()
    if removed:
        click.echo(f"\nGC: pruned {removed} old completed assignment(s)")

    save_board(board)
    click.echo(f"\nBoard saved ({len(board.active)} active, {len(board.completed)} completed)")

    if board.active:
        click.echo("\nActive assignments:")
        for a in board.active:
            click.echo(f"  {a.machine_name} → {a.repo_name} #{a.issue_number}: {a.issue_title}")


@main.command(help="Pull a worker's branch locally for smoke testing.")
@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option("--passed", "verdict", flag_value="pass", help="Mark smoke test as passed.")
@click.option("--fail", "verdict", flag_value="fail", help="Mark smoke test as failed.")
@click.option("--reason", default="", help="Reason for failure (used with --fail).")
def test(assignment_id: str, config_path: Path, verdict: str | None, reason: str) -> None:
    from coord.state import build_board, load_board, save_board

    cfg = _load_config(config_path)
    board = load_board() or build_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    repo = cfg.repo(assignment.repo_name)

    # ── Record verdict ──────────────────────────────────────────────────
    if verdict:
        assignment.smoke_test = verdict
        assignment.smoke_test_reason = reason if verdict == "fail" else None
        save_board(board)
        if verdict == "pass":
            click.echo(f"Smoke test PASSED for {assignment.repo_name} #{assignment.issue_number}")
        else:
            click.echo(f"Smoke test FAILED for {assignment.repo_name} #{assignment.issue_number}")
            if reason:
                click.echo(f"  reason: {reason}")
        return

    # ── Checkout and build ──────────────────────────────────────────────
    if not assignment.branch:
        click.echo(
            f"error: assignment {assignment_id} has no branch recorded. "
            f"The worker may not have pushed yet, or the branch wasn't captured during reconciliation.",
            err=True,
        )
        sys.exit(1)

    import socket
    import subprocess

    hostname = socket.gethostname().split(".")[0]
    local_machine = next(
        (m for m in cfg.machines if m.name == hostname or m.host.split(".")[0] == hostname),
        None,
    )
    repo_path = None
    if local_machine:
        repo_path = local_machine.repo_path(assignment.repo_name)
    if repo_path is None:
        for m in cfg.machines:
            repo_path = m.repo_path(assignment.repo_name)
            if repo_path:
                break
    if repo_path is None:
        click.echo(
            f"error: no repo_path configured for {assignment.repo_name!r}. "
            f"Add it to coordinator.yml under machines[].repo_paths.",
            err=True,
        )
        sys.exit(1)

    from pathlib import Path as P
    repo_dir = P(repo_path).expanduser()
    if not repo_dir.exists():
        click.echo(f"error: repo path does not exist: {repo_dir}", err=True)
        sys.exit(1)

    click.echo(f"Fetching and checking out branch {assignment.branch!r} in {repo_dir}...")
    try:
        subprocess.run(
            ["git", "fetch", "origin"], cwd=str(repo_dir),
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "checkout", assignment.branch], cwd=str(repo_dir),
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        click.echo(f"error: git command failed: {e.stderr.strip()}", err=True)
        sys.exit(1)

    click.echo(f"Branch {assignment.branch!r} checked out.")

    if repo and repo.build_command:
        click.echo(f"Running build: {repo.build_command}")
        result = subprocess.run(
            repo.build_command, shell=True, cwd=str(repo_dir),
        )
        if result.returncode != 0:
            click.echo(f"Build failed (exit {result.returncode})", err=True)
            sys.exit(1)
        click.echo("Build succeeded.")

    if repo and repo.test_command:
        click.echo(f"Running tests: {repo.test_command}")
        result = subprocess.run(
            repo.test_command, shell=True, cwd=str(repo_dir),
        )
        if result.returncode != 0:
            click.echo(f"Tests failed (exit {result.returncode})", err=True)
            sys.exit(1)
        click.echo("Tests passed.")

    click.echo(
        f"\nReady for smoke test. Run:\n"
        f"  coord test --passed {assignment_id}   # if it looks good\n"
        f"  coord test --fail {assignment_id} --reason \"description\"   # if not"
    )


@main.command(help="Create sub-issues from a split proposal (e.g. coord split S1).")
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


@main.command(help="End the session — run housekeeping hooks and show summary.")
@_CONFIG_OPTION
def done(config_path: Path) -> None:
    from coord.hooks import run_hooks
    from coord.state import build_board, load_board, save_board

    cfg = _load_config(config_path)
    board = load_board() or build_board()

    if board.active:
        click.echo(
            f"warning: {len(board.active)} assignment(s) still active. "
            f"They will continue running on their agent servers.",
            err=True,
        )

    if cfg.hooks.on_session_end:
        click.echo("Running session-end hooks:")
        for result in run_hooks("on_session_end", cfg, board):
            status = "ok" if result.ok else "FAILED"
            click.echo(f"  [{status}] {result.hook}: {result.message}")
    else:
        from coord.hooks import _summary_report
        click.echo(_summary_report(cfg, board))

    save_board(board)
    click.echo("\nSession ended. Board saved.")


@main.command(help="Start the web dashboard (port 7434).")
def web() -> None:
    _not_implemented("web")


if __name__ == "__main__":
    main()
