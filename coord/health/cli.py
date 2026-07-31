"""``coord health`` — run the check registry against this machine (#1628).

Deliberately the *only* consumer of the engine in this child.  No transport,
no board write, no daemon: H-1 ends at a CLI you can run by hand.  H-3 adds
the fleet-scope probes and the board projection; H-4 adds renderers beyond
this one.  Both consume ``--json``, whose shape is
:meth:`coord.health.registry.HealthReport.to_dict`.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from coord.commands._common import _CONFIG_OPTION
from coord.health.context import build_context
from coord.health.models import Severity
from coord.health.registry import run_all
from coord.health.render import render_report

# Exit codes under --exit-code.  Chosen so a timer can distinguish "needs a
# human now" from "worth a look" without parsing anything.
EXIT_OK = 0
EXIT_WARN = 1
EXIT_CRIT = 2


@click.command("health")
@_CONFIG_OPTION
@click.option(
    "--local",
    is_flag=True,
    default=False,
    help=(
        "Restrict to machine- and checkout-scope checks (this machine and its "
        "checkouts). Fleet-scope probes do not exist yet, so today this is "
        "equivalent to the default — pass it to keep the intent explicit."
    ),
)
@click.option("--json", "output_json", is_flag=True, default=False,
              help="Emit the machine-readable report instead of the text one.")
@click.option(
    "--no-network",
    is_flag=True,
    default=False,
    help=(
        "Skip probes that leave the machine (the PyPI simple-index fetch and "
        "the Max-plan usage probe). Skipped checks are listed, never silently "
        "reported as healthy."
    ),
)
@click.option("--check", "only", multiple=True,
              help="Run only these check ids (repeatable).")
@click.option("-v", "--verbose", is_flag=True, default=False,
              help="Show each check's detail/fix line, including for healthy ones.")
@click.option(
    "--exit-code",
    is_flag=True,
    default=False,
    help=f"Exit {EXIT_CRIT} on any CRIT, {EXIT_WARN} on any WARN (default: always 0).",
)
def health(
    config_path: Path,
    local: bool,
    output_json: bool,
    no_network: bool,
    only: tuple[str, ...],
    verbose: bool,
    exit_code: bool,
) -> None:
    """Report how much headroom is left on this machine and its checkouts.

    Read-only and non-destructive by construction: every probe stats, reads,
    or shells out to a read-only command. Nothing here writes, prunes, or
    remediates — deciding what may self-heal is a separate call.
    """
    config = _load_config_or_none(config_path)
    ctx = build_context(config, allow_network=not no_network)

    if not getattr(ctx.thresholds, "enabled", True):
        # A disabled engine still reports *that* it is disabled — a health
        # check silently switched off looks identical to a healthy fleet.
        if output_json:
            click.echo(json.dumps({"schema": 1, "enabled": False, "results": []}))
        else:
            click.echo("health checks disabled in coordinator.yml (health.enabled: false)")
        return

    scopes = ("machine", "checkout") if local else None
    report = run_all(ctx, scopes=scopes, only=list(only) or None)

    if output_json:
        click.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        click.echo(render_report(report, verbose=verbose))

    if exit_code:
        if report.severity is Severity.CRIT:
            raise SystemExit(EXIT_CRIT)
        if report.severity is Severity.WARN:
            raise SystemExit(EXIT_WARN)


def _load_config_or_none(config_path: Path):
    """Load ``coordinator.yml``, or carry on without one.

    A machine with no config still has disks, worktrees, a venv and a
    ``claude`` binary — refusing to report any of that because a YAML file is
    missing would make the check useless on exactly the machine most likely
    to be misconfigured.
    """
    from coord.config import ConfigError, load  # noqa: PLC0415

    try:
        return load(config_path)
    except (ConfigError, OSError) as exc:
        click.echo(f"note: no usable coordinator.yml ({exc}); "
                   f"running machine-scope checks only", err=True)
        return None
