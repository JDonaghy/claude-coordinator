"""Session/daemon lifecycle commands: `notify`, `resume`, `done`, `web`,
`serve`, `housekeeping`. Extracted from coord/cli.py (#747)."""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import click


from coord.commands._common import SERVE_PORT, _CONFIG_OPTION, _load_config


def _print_housekeeping_result(resp: dict) -> None:
    dry = resp.get("dry_run")
    archived_a = resp.get("archived_assignments", 0)
    archived_n = resp.get("archived_notifications", 0)
    days = resp.get("retention_days")
    if not archived_a and not archived_n:
        click.echo(
            f"housekeeping: nothing to archive (no terminal rows older than {days}d)."
        )
        return
    verb = "would archive" if dry else "archived"
    suffix = "  (dry-run — nothing moved)" if dry else ""
    click.echo(
        f"housekeeping: {verb} {archived_a} assignment(s) + "
        f"{archived_n} notification(s) (terminal, older than {days}d).{suffix}"
    )


@click.command(
    "housekeeping",
    help=(
        "#762: archive stale terminal board rows so the /board payload + DB stay "
        "bounded (an unbounded board overran the TUI fetch timeout and blanked "
        "the board).\n\n"
        "Moves terminal assignments older than COORD_ARCHIVE_RETENTION_DAYS "
        "(default 30) + their notifications into assignments_archive / "
        "notifications_archive — it NEVER deletes, and never touches active, "
        "recent, merge-queued, open-issue-latest, or review-linked rows. Routes "
        "through the daemon (the canonical DB lives there)."
    ),
)


@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would be archived without moving anything.",
)


def housekeeping(dry_run: bool) -> None:
    """#762: archive stale terminal board rows (active/recent/referenced kept)."""
    from coord.board_service import daemon_reroute_target  # noqa: PLC0415

    _svc = daemon_reroute_target("COORD_HOUSEKEEPING_ON_DAEMON")
    if _svc is not None:
        from coord.client import post_record  # noqa: PLC0415

        try:
            resp = post_record(_svc, "/housekeeping", {"dry_run": dry_run}, timeout=180.0)
        except Exception as exc:  # noqa: BLE001
            click.echo(f"error: housekeeping via daemon failed: {exc}", err=True)
            sys.exit(1)
        _print_housekeeping_result(resp)
        return

    from coord import housekeeping as _hk  # noqa: PLC0415

    _print_housekeeping_result(_hk.sweep(dry_run=dry_run))


@click.command(help="Poll agents and post completion/failure comments on GitHub.")
@_CONFIG_OPTION
def notify(config_path: Path) -> None:
    # #906: `coord notify` reads dispatched assignments and writes back
    # mark_notified/save_plan/update_claude_session_id — all local-DB
    # operations that are empty/no-op on a thin client.  Route the whole
    # command to the daemon so it runs against the canonical DB + real agent
    # fleet.  COORD_NOTIFY_ON_DAEMON guards the daemon against re-routing to
    # itself (same pattern as coord merge / reconcile-merges / diagnose /
    # housekeeping).
    from coord.board_service import daemon_reroute_target  # noqa: PLC0415

    _svc = daemon_reroute_target("COORD_NOTIFY_ON_DAEMON")
    if _svc is not None:
        from coord.client import post_record  # noqa: PLC0415

        try:
            resp = post_record(_svc, "/notify", {}, timeout=180.0)
        except Exception as exc:  # noqa: BLE001
            click.echo(f"error: notify via daemon failed: {exc}", err=True)
            sys.exit(1)
        output = resp.get("output") or ""
        if output:
            click.echo(output, nl=False)
        if resp.get("error"):
            click.echo(f"error: {resp['error']}", err=True)
        code = resp.get("exit_code") or 0
        if code:
            sys.exit(int(code))
        return

    from coord.board_service import read_board, write_board
    from coord.hooks import is_round_complete, run_hooks
    from coord.notify import run as run_notify

    cfg = _load_config(config_path)
    posted, stuck, needs_attention = run_notify(cfg)
    if not posted and not stuck and not needs_attention:
        click.echo("No new transitions to notify.")
        return
    if posted:
        click.echo(f"Posted {len(posted)} completion/failure comment(s):")
        for t in posted:
            click.echo(
                f"  [{t.event}] {t.machine_name} → {t.repo_name} "
                f"#{t.issue_number} (assignment {t.assignment_id}, exit {t.exit_code})"
            )
    if stuck:
        click.echo(f"Posted {len(stuck)} stuck detection(s):")
        for s in stuck:
            click.echo(
                f"  [stuck] {s.machine_name} → {s.repo_name} "
                f"#{s.issue_number} (assignment {s.assignment_id})"
            )
            click.echo(f"    {s.stuck_message}")
    if needs_attention:
        click.echo(f"Posted {len(needs_attention)} needs-attention detection(s):")
        for n in needs_attention:
            click.echo(
                f"  [needs-attention:{n.reason}] {n.machine_name} → {n.repo_name} "
                f"#{n.issue_number} (assignment {n.assignment_id})"
            )
            click.echo(f"    {n.detail}")
    board = read_board()

    if is_round_complete(board) and cfg.hooks.on_round_complete:
        click.echo("\nRound complete — running hooks:")
        for result in run_hooks("on_round_complete", cfg, board):
            status = "ok" if result.ok else "FAILED"
            click.echo(f"  [{status}] {result.hook}: {result.message}")

    write_board(board)


@click.command(help="Recover board state after a crash or restart.")
@_CONFIG_OPTION
def resume(config_path: Path) -> None:
    from coord.board_service import is_remote, read_board, write_board
    from coord.reconcile import reconcile

    cfg = _load_config(config_path)
    if not is_remote():
        # Informational only (local mode): distinguish "no board saved yet" from
        # "loaded an existing board" — read_board() below does the actual
        # local/remote read.
        from coord.state import load_board as _peek_local_board  # noqa: PLC0415

        if _peek_local_board() is None:
            click.echo("No saved board found. Rebuilding from dispatched ledger...")
    board = read_board()

    click.echo(f"Board round: {board.round_number}")
    click.echo(f"  active:    {len(board.active)} assignment(s)")
    click.echo(f"  completed: {len(board.completed)} assignment(s)")

    if board.active:
        click.echo("\nReconciling with agent servers...")
        changed = reconcile(board, cfg)
        if changed:
            click.echo(f"  {len(changed)} assignment(s) finished since last check:")
            from coord.merge_queue import enqueue as _mq_enqueue
            for aid in changed:
                a = board.find_by_id(aid)
                if a:
                    click.echo(f"    {a.machine_name} → {a.repo_name} #{a.issue_number}: [{a.status}]")
                    if a.status == "done":
                        repo_cfg = cfg.repo(a.repo_name)
                        if repo_cfg is not None and a.branch:
                            entry = _mq_enqueue(
                                a,
                                repo_github=repo_cfg.github,
                                target_branch=repo_cfg.default_branch,
                            )
                            if entry is not None:
                                click.echo(
                                    f"      → enqueued for merge ({entry.branch} → {entry.target_branch})"
                                )
                        elif a.status == "done" and not a.branch:
                            click.echo(
                                "      → no branch captured; skip merge enqueue"
                            )
        else:
            click.echo("  all active assignments still running")

    removed = board.gc()
    if removed:
        click.echo(f"\nGC: pruned {removed} old completed assignment(s)")

    write_board(board)
    click.echo(f"\nBoard saved ({len(board.active)} active, {len(board.completed)} completed)")

    if board.active:
        click.echo("\nActive assignments:")
        for a in board.active:
            click.echo(f"  {a.machine_name} → {a.repo_name} #{a.issue_number}: {a.issue_title}")


@click.command(help="End the session — run housekeeping hooks and show summary.")
@_CONFIG_OPTION
def done(config_path: Path) -> None:
    from coord.board_service import read_board, write_board
    from coord.hooks import run_hooks

    cfg = _load_config(config_path)
    board = read_board()

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

    # Repo housekeeping: pull latest and run configured commands
    hostname = socket.gethostname().split(".")[0]
    local_machine = next(
        (m for m in cfg.machines if m.name == hostname or m.host.split(".")[0] == hostname),
        None,
    )

    if local_machine:
        for repo in cfg.repos:
            if not repo.housekeeping:
                continue
            repo_path_str = local_machine.repo_path(repo.name)
            if not repo_path_str:
                click.echo(f"  {repo.name}: no local path configured, skipping housekeeping")
                continue
            repo_path = Path(repo_path_str).expanduser()
            if not repo_path.exists():
                click.echo(f"  {repo.name}: path {repo_path} does not exist, skipping")
                continue

            # Pull latest
            click.echo(f"\n{repo.name}: pulling latest...")
            try:
                subprocess.run(
                    ["git", "pull", "--ff-only"],
                    cwd=str(repo_path), check=True, capture_output=True, text=True,
                )
            except subprocess.CalledProcessError as e:
                click.echo(f"  git pull failed: {e.stderr.strip()}", err=True)
                # Continue with housekeeping anyway — might still work

            # Run housekeeping commands
            for cmd in repo.housekeeping:
                click.echo(f"  running: {cmd}")
                try:
                    result = subprocess.run(
                        cmd, shell=True, cwd=str(repo_path),
                        capture_output=True, text=True, timeout=300,
                    )
                    if result.returncode != 0:
                        click.echo(f"  failed (exit {result.returncode}): {result.stderr.strip()}", err=True)
                    else:
                        click.echo(f"  done")
                except subprocess.TimeoutExpired:
                    click.echo(f"  timed out after 300s", err=True)
                except Exception as e:
                    click.echo(f"  error: {e}", err=True)
    else:
        click.echo("\nCould not determine local machine — skipping repo housekeeping")

    write_board(board)

    # Write session end summary — use the usage module so the output matches `coord usage`.
    import datetime
    from coord.state import write_session_end, load_session
    from coord.usage import build_session_usage, format_usage_report

    sess = load_session()
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
    total_cost = session_usage.total_cost_usd

    click.echo("")
    click.echo(format_usage_report(session_usage))

    completed_ids = [a.assignment_id for a in board.completed if a.assignment_id]
    issues_closed = list(set(a.issue_number for a in board.completed))
    write_session_end(
        completed_ids=completed_ids,
        issues_closed=issues_closed,
        total_cost_usd=total_cost,
    )
    click.echo(f"\nSession saved (${total_cost:.2f} total cost)")

    click.echo("\nSession ended. Board saved.")


@click.command(help="Start the web dashboard (port 7434).")
@_CONFIG_OPTION
@click.option("--host", "bind_host", default="0.0.0.0", show_default=True)
@click.option("--port", "bind_port", default=7434, show_default=True, type=int)
def web(config_path: Path, bind_host: str, bind_port: int) -> None:
    import uvicorn
    from coord.dashboard.server import build_app

    cfg = _load_config(config_path)
    app = build_app(cfg)
    click.echo(f"coord web: dashboard at http://{bind_host}:{bind_port}")
    uvicorn.run(app, host=bind_host, port=bind_port, log_level="info")


@click.command(
    help=(
        "Start the portable control-center daemon (#584, port 7435).  Serves the "
        "board (GET /board) + config (GET /config) and records results (POST "
        "/result, /completion, #590) against the one shared ~/.coord/coord.db, so "
        "any Tailscale machine renders and drives the same board.  Run this on "
        "the always-on host that owns the DB.  Optional bearer token (flag > "
        "$COORD_SERVE_TOKEN > ~/.coord/serve_token)."
    )
)


@_CONFIG_OPTION
@click.option("--host", "bind_host", default="0.0.0.0", show_default=True)
@click.option("--port", "bind_port", default=SERVE_PORT, show_default=True, type=int)
@click.option(
    "--token",
    "token",
    default=None,
    envvar="COORD_SERVE_TOKEN",
    help=(
        "Shared bearer token; clients must send Authorization: Bearer <token>. "
        "Resolves flag > $COORD_SERVE_TOKEN > ~/.coord/serve_token. Prefer the "
        "file/env (a --token on the command line leaks via `ps`). Unset → open "
        "(tailnet ACL only)."
    ),
)


def serve(config_path: Path, bind_host: str, bind_port: int, token: str | None) -> None:
    import uvicorn

    from coord.dao import SqliteStore
    from coord.db import DB_PATH
    from coord.serve_app import build_app as build_serve_app
    from coord.serve_app import resolve_serve_token

    cfg = _load_config(config_path)
    token = resolve_serve_token(token)
    store = SqliteStore(DB_PATH)
    app = build_serve_app(store, cfg, token=token)
    auth = "bearer-token" if token else "OPEN (tailnet ACL only)"
    click.echo(
        f"coord serve: control center at http://{bind_host}:{bind_port} "
        f"(config={cfg.path}, db={DB_PATH}, auth={auth})"
    )
    if not token:
        click.echo(
            "  warning: no bearer token — endpoints are open to anyone who can "
            "reach this port. Fine for dev; the production daemon should set one "
            "(echo <secret> > ~/.coord/serve_token). See AGENT_OPERATIONS.md.",
            err=True,
        )
    uvicorn.run(app, host=bind_host, port=bind_port, log_level="info")