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
def status(config_path: Path) -> None:
    cfg = _load_config(config_path)
    click.echo("Machines:")
    for machine in cfg.machines:
        try:
            resp = httpx.get(
                f"http://{machine.host}:{AGENT_PORT}/status", timeout=5,
            )
            data = resp.json()
            assignment = data.get("assignment")
            if assignment:
                state = f"busy — #{assignment['issue_number']}: {assignment.get('issue_title', '?')}"
            else:
                state = "idle"
        except (httpx.HTTPError, httpx.TimeoutException):
            state = "offline"
        repos = ", ".join(machine.repos) if machine.repos else "(none)"
        click.echo(f"  {machine.name:15s} [{state}]")
        click.echo(f"    host: {machine.host}  repos: {repos}")


@main.command(help="Brain proposes assignments for idle machines.")
@_CONFIG_OPTION
@click.option("--dry-run", is_flag=True, help="Plan without saving proposals.")
def plan(config_path: Path, dry_run: bool) -> None:
    from coord.brain import propose
    from coord.state import save_proposals

    cfg = _load_config(config_path)
    click.echo("Gathering context and calling Claude...\n")

    try:
        proposals = propose(cfg)
    except RuntimeError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    if not proposals:
        click.echo("No assignments to propose.")
        return

    click.echo(f"{len(proposals)} proposal(s):\n")
    for p in proposals:
        click.echo(f"  [{p.id}] {p.machine_name} → {p.repo_name} #{p.issue_number}: {p.issue_title}")
        click.echo(f"      {p.rationale}")
        if p.files_likely:
            click.echo(f"      files: {', '.join(p.files_likely)}")
        click.echo()

    if dry_run:
        click.echo("(dry run — proposals not saved)")
    else:
        path = save_proposals(proposals)
        click.echo(f"Proposals saved to {path}")
        click.echo("Run `coord approve <ids>` to dispatch (e.g. coord approve 1,2)")


@main.command(help="Dispatch approved assignments (comma-separated IDs).")
@click.argument("ids")
@_CONFIG_OPTION
@click.option("--dry-run", is_flag=True, help="Show what would be dispatched.")
def approve(ids: str, config_path: Path, dry_run: bool) -> None:
    from coord.dispatch import dispatch, post_briefing
    from coord.state import load_proposals, clear_proposals

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

    for p in selected:
        click.echo(f"[{p.id}] {p.machine_name} → {p.repo_name} #{p.issue_number}: {p.issue_title}")
        if dry_run:
            click.echo("     (dry run — not dispatched)")
            continue
        try:
            dispatch(p, cfg)
            click.echo("     dispatched to agent server")
        except Exception as e:
            click.echo(f"     dispatch failed: {e}", err=True)
            continue
        try:
            post_briefing(p, cfg)
            click.echo("     briefing posted to GitHub")
        except Exception as e:
            click.echo(f"     briefing post failed: {e}", err=True)

    if not dry_run:
        clear_proposals()
        click.echo("\nPending proposals cleared.")


@main.command(help="Start the web dashboard (port 7434).")
def web() -> None:
    _not_implemented("web")


if __name__ == "__main__":
    main()
