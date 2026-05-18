"""Click CLI entry point for the `coord` command."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from coord import __version__
from coord.config import Config, ConfigError, DEFAULT_CONFIG_PATH, load


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
def agent() -> None:
    _not_implemented("agent")


@main.command(help="Show all machines, assignments, and connectivity.")
@_CONFIG_OPTION
def status(config_path: Path) -> None:
    _load_config(config_path)
    _not_implemented("status")


@main.command(help="Brain proposes assignments for idle machines.")
@_CONFIG_OPTION
@click.option("--dry-run", is_flag=True, help="Plan without saving proposals.")
def plan(config_path: Path, dry_run: bool) -> None:
    _load_config(config_path)
    _not_implemented("plan")


@main.command(help="Dispatch approved assignments (comma-separated IDs).")
@click.argument("ids")
@click.option("--dry-run", is_flag=True, help="Show what would be dispatched.")
def approve(ids: str, dry_run: bool) -> None:
    _not_implemented("approve")


@main.command(help="Start the web dashboard (port 7434).")
def web() -> None:
    _not_implemented("web")


if __name__ == "__main__":
    main()
