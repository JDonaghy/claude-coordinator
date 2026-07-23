"""The `coord agent` group: per-machine agent server lifecycle
(start/update/restart/clean-worktrees) plus `pause`/`unpause`.
Extracted from coord/cli.py (#747)."""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

import click
import httpx

from coord.config import Config

from coord.commands._common import AGENT_PORT, _CONFIG_OPTION, _load_config


@click.group(
    invoke_without_command=True,
    help=(
        "Agent server management.  Without a subcommand, starts the agent "
        "server on this machine (port 7433)."
    ),
)


@_CONFIG_OPTION
@click.option(
    "--machine",
    "machine_name",
    default=None,
    help="Machine name from coordinator.yml (defaults to hostname match).",
)


@click.option("--host", "bind_host", default="0.0.0.0", show_default=True)
@click.option("--port", "bind_port", default=AGENT_PORT, show_default=True, type=int)
@click.pass_context
def agent(
    ctx: click.Context,
    config_path: Path,
    machine_name: str | None,
    bind_host: str,
    bind_port: int,
) -> None:
    ctx.ensure_object(dict)
    ctx.obj.update(
        config_path=config_path,
        machine_name=machine_name,
        bind_host=bind_host,
        bind_port=bind_port,
    )
    if ctx.invoked_subcommand is None:
        _start_agent_server(config_path, machine_name, bind_host, bind_port)


def _start_agent_server(
    config_path: Path,
    machine_name: str | None,
    bind_host: str,
    bind_port: int,
) -> None:
    """Internal helper: start the uvicorn-backed agent server."""
    import uvicorn

    from coord.agent import AgentServer
    from coord.agent_app import build_app

    # Config-free mode: when --machine is supplied and coordinator.yml doesn't
    # exist (typical on a dedicated worker node), run with empty capabilities
    # and repos. The coordinator sends repo details at dispatch time.
    from coord.config import ConcurrencyConfig as _ConcurrencyConfig
    from coord.providers import build_provider as _build_provider
    concurrency = _ConcurrencyConfig()
    artifact_paths_by_repo: dict[str, list[str]] = {}
    build_commands_by_repo: dict[str, str] = {}
    # #425: providers registry from cfg.providers.definitions.  Empty when
    # there's no config file (config-free mode) — the agent then runs with
    # no providers and the legacy claude -p spawn path, byte-identical to
    # pre-#425 behaviour.
    providers_registry: dict[str, object] = {}
    if not config_path.exists() and machine_name:
        from coord.models import Machine as _Machine
        machine = _Machine(
            name=machine_name,
            host="localhost",
            capabilities=[],
            repos=[],
            repo_paths={},
        )
    else:
        cfg = _load_config(config_path)
        machine = _resolve_machine(cfg, machine_name)
        concurrency = cfg.concurrency
        # #305: collect artifact_paths per repo for the stash helper.
        artifact_paths_by_repo = {
            r.name: r.artifact_paths
            for r in cfg.repos
            if r.artifact_paths
        }
        # #1323 (fix #3): collect build_command per repo so _stash_artifacts
        # can run it in the worktree before globbing, ensuring the binary
        # exists regardless of the worker's dev-loop feature flags.
        build_commands_by_repo = {
            r.name: r.build_command
            for r in cfg.repos
            if r.build_command
        }
        # #425: instantiate each named provider so the agent can dispatch
        # to it when an assignment names it (spec.provider).  An unknown
        # provider type raises ValueError from build_provider — surface
        # it as a startup failure rather than silently dropping the
        # definition, so operators notice misconfiguration early.
        for prov_name, defn in cfg.providers.definitions.items():
            providers_registry[prov_name] = _build_provider(
                prov_name, defn, cfg.models
            )

    server = AgentServer(
        machine_name=machine.name,
        capabilities=machine.capabilities,
        repos=machine.repos,
        repo_paths=machine.repo_paths,
        bash_wrap_spawn=concurrency.bash_wrap_spawn,
        first_output_timeout=concurrency.first_output_timeout,
        artifact_paths=artifact_paths_by_repo,
        build_commands=build_commands_by_repo,
        providers=providers_registry,
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


@agent.command(
    "update",
    help=(
        "POST /update to one or all agent servers.  The agent upgrades the "
        "claude-coordinator package (git pull for editable installs, "
        "pip install --upgrade otherwise) then restarts itself.  Waits up to "
        "--timeout seconds for the agent(s) to come back online."
    ),
)


@_CONFIG_OPTION
@click.option(
    "--machine",
    "machine_filter",
    default=None,
    help="Name of a single machine to update (from coordinator.yml).",
)


@click.option(
    "--all",
    "all_machines",
    is_flag=True,
    help="Update all machines (mutually exclusive with --machine).",
)


@click.option(
    "--timeout",
    default=120,
    show_default=True,
    type=int,
    help="Seconds to wait for the agent to come back online after restart.",
)


def agent_update(
    config_path: Path,
    machine_filter: str | None,
    all_machines: bool,
    timeout: int,
) -> None:
    cfg = _load_config(config_path)
    targets = _resolve_agent_targets(cfg, machine_filter, all_machines)
    if not targets:
        click.echo("No machines to update.", err=True)
        sys.exit(2)

    # Capture each agent's start time BEFORE we trigger /update so the
    # wait loop can distinguish "old agent still answering during pip"
    # from "new agent came back up".
    pre_started_at = _fetch_pre_started_at(targets)

    for machine in targets:
        url = f"http://{machine.host}:{AGENT_PORT}/update"
        click.echo(f"  {machine.name}: POST {url} ...", nl=False)
        try:
            resp = httpx.post(url, timeout=10)
            if resp.status_code == 202:
                data = resp.json()
                click.echo(f" accepted (mode: {data.get('mode', '?')})")
            else:
                click.echo(f" HTTP {resp.status_code}")
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            click.echo(f" error: {e}")

    if targets:
        click.echo(f"\nWaiting up to {timeout}s for agent(s) to come back online...")
        results = _wait_agents_online(
            targets, timeout=timeout, pre_started_at=pre_started_at
        )
        for name, came_back in results.items():
            tag = "✓ online" if came_back else "✗ did not come back"
            click.echo(f"  {name}: {tag}")
        # Fetch each agent's /health to report what actually happened —
        # version delta or the recorded failure reason.
        click.echo("")
        for machine in targets:
            if not results.get(machine.name):
                continue
            try:
                resp = httpx.get(
                    f"http://{machine.host}:{AGENT_PORT}/health",
                    timeout=5,
                )
                health = resp.json() if resp.status_code == 200 else {}
            except (httpx.HTTPError, httpx.TimeoutException):
                health = {}
            version_now = health.get("version") or "?"
            last = health.get("last_update") or {}
            result = last.get("result")
            if result == "upgraded":
                vbefore = last.get("version_before", "?")
                vafter = last.get("version_after", version_now)
                click.echo(f"  {machine.name}: {vbefore} → {vafter}")
            elif result == "no_change":
                click.echo(
                    f"  {machine.name}: no change (still {version_now}) — "
                    f"{last.get('error', 'pip resolved to the same version')}"
                )
            elif result == "failed":
                err = last.get("error") or "pip failed; see ~/.coord/last_update.log"
                click.echo(f"  {machine.name}: ✗ failed — {err}", err=True)
            else:
                # Old agent build (no last_update payload yet) — just
                # report the version it's now reporting.
                click.echo(f"  {machine.name}: now reporting v{version_now}")
        if not all(results.values()):
            sys.exit(1)


@agent.command(
    "restart",
    help=(
        "POST /restart to one or all agent servers.  The agent waits for "
        "active workers to finish (or cancels them after --cancel-timeout "
        "seconds) then restarts itself.  Waits up to --timeout seconds for "
        "the agent(s) to come back online."
    ),
)


@_CONFIG_OPTION
@click.option(
    "--machine",
    "machine_filter",
    default=None,
    help="Name of a single machine to restart (from coordinator.yml).",
)


@click.option(
    "--all",
    "all_machines",
    is_flag=True,
    help="Restart all machines (mutually exclusive with --machine).",
)


@click.option(
    "--timeout",
    default=120,
    show_default=True,
    type=int,
    help="Seconds to wait for the agent to come back online after restart.",
)


@click.option(
    "--cancel-timeout",
    default=30,
    show_default=True,
    type=int,
    help="Seconds the agent waits for active workers to finish before cancelling them.",
)


def agent_restart(
    config_path: Path,
    machine_filter: str | None,
    all_machines: bool,
    timeout: int,
    cancel_timeout: int,
) -> None:
    cfg = _load_config(config_path)
    targets = _resolve_agent_targets(cfg, machine_filter, all_machines)
    if not targets:
        click.echo("No machines to restart.", err=True)
        sys.exit(2)

    for machine in targets:
        url = f"http://{machine.host}:{AGENT_PORT}/restart"
        click.echo(f"  {machine.name}: POST {url} ...", nl=False)
        try:
            resp = httpx.post(
                url,
                json={"cancel_timeout": cancel_timeout},
                timeout=10,
            )
            if resp.status_code == 202:
                data = resp.json()
                active = data.get("active_workers", 0)
                click.echo(f" accepted ({active} active worker(s))")
            else:
                click.echo(f" HTTP {resp.status_code}")
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            click.echo(f" error: {e}")

    if targets:
        click.echo(f"\nWaiting up to {timeout}s for agent(s) to come back online...")
        results = _wait_agents_online(targets, timeout=timeout)
        for name, came_back in results.items():
            tag = "✓ online" if came_back else "✗ did not come back"
            click.echo(f"  {name}: {tag}")
        if not all(results.values()):
            sys.exit(1)


@agent.command(
    "clean-worktrees",
    help=(
        "POST /worktree-clean to one or all agent servers.  Each agent "
        "removes git worktrees whose assignment is in a terminal state "
        "(done/failed/cancelled) and finished more than --recent-secs ago.  "
        "Running/pending worktrees are never touched."
    ),
)


@_CONFIG_OPTION
@click.option(
    "--machine",
    "machine_filter",
    default=None,
    help="Name of a single machine to clean (from coordinator.yml).",
)


@click.option(
    "--all",
    "all_machines",
    is_flag=True,
    help="Clean all machines (mutually exclusive with --machine).",
)


@click.option(
    "--recent-secs",
    default=300,
    show_default=True,
    type=int,
    help=(
        "Minimum age in seconds for a terminal assignment's worktree to be "
        "eligible for removal (guards against racing with a just-finished worker)."
    ),
)


def agent_clean_worktrees(
    config_path: Path,
    machine_filter: str | None,
    all_machines: bool,
    recent_secs: int,
) -> None:
    cfg = _load_config(config_path)
    targets = _resolve_agent_targets(cfg, machine_filter, all_machines)
    if not targets:
        click.echo("No machines to clean.", err=True)
        sys.exit(2)

    any_error = False
    for machine in targets:
        url = f"http://{machine.host}:{AGENT_PORT}/worktree-clean"
        click.echo(f"  {machine.name}: POST {url} ...", nl=False)
        try:
            resp = httpx.post(url, json={"recent_secs": recent_secs}, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                cleaned = data.get("cleaned", 0)
                kept = data.get("kept", 0)
                freed = data.get("bytes_freed", 0)
                freed_mb = freed / (1024 * 1024)
                click.echo(
                    f" cleaned={cleaned} kept={kept} freed={freed_mb:.1f} MB"
                )
            else:
                click.echo(f" HTTP {resp.status_code}")
                any_error = True
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            click.echo(f" error: {e}")
            any_error = True

    if any_error:
        sys.exit(1)


def _resolve_agent_targets(cfg, machine_filter: str | None, all_machines: bool):
    """Return the list of Machine objects to target for update/restart.

    Validates --machine / --all flags and prints errors on bad input.
    """
    if machine_filter and all_machines:
        click.echo("error: --machine and --all are mutually exclusive.", err=True)
        sys.exit(2)
    if not machine_filter and not all_machines:
        click.echo(
            "error: specify either --machine NAME or --all.", err=True
        )
        sys.exit(2)

    if machine_filter:
        machine = next((m for m in cfg.machines if m.name == machine_filter), None)
        if machine is None:
            click.echo(
                f"error: machine {machine_filter!r} not in coordinator.yml "
                f"(have: {[m.name for m in cfg.machines]})",
                err=True,
            )
            sys.exit(2)
        return [machine]

    return list(cfg.machines)


def _wait_agents_online(
    machines: list,
    *,
    timeout: float = 120.0,
    poll_interval: float = 2.0,
    pre_started_at: dict[str, float | None] | None = None,
) -> dict[str, bool]:
    """Poll /health on each machine until all are online or timeout expires.

    When ``pre_started_at`` is provided, a machine is only considered
    "back" once its reported ``agent_started_at`` differs from the
    pre-update value (or appears for the first time on an agent that
    didn't expose it before).  This stops the CLI from racing the old
    agent while a pip upgrade is still running inside it.

    For agents that don't expose ``agent_started_at`` at all (pre-v0.4.3),
    we fall back to "responding to /health is enough."

    Returns ``{machine_name: came_back_online}`` for every machine.
    """
    deadline = time.time() + timeout
    online: set[str] = set()
    pre = pre_started_at or {}

    while time.time() < deadline:
        for machine in machines:
            if machine.name in online:
                continue
            try:
                resp = httpx.get(
                    f"http://{machine.host}:{AGENT_PORT}/health",
                    timeout=3.0,
                )
                if resp.status_code != 200:
                    continue
                if machine.name in pre:
                    pre_val = pre[machine.name]
                    try:
                        cur = resp.json().get("agent_started_at")
                    except Exception:
                        cur = None
                    if cur is None:
                        # Old agent (no started_at) — fall back to "alive
                        # is good enough" so /update on a pre-v0.4.3
                        # agent isn't blocked forever.
                        online.add(machine.name)
                    elif pre_val is None or cur != pre_val:
                        # Either the agent didn't expose started_at
                        # before (just upgraded TO v0.4.3) or the value
                        # changed (restart happened).
                        online.add(machine.name)
                else:
                    online.add(machine.name)
            except Exception:
                pass

        if len(online) == len(machines):
            break
        time.sleep(poll_interval)

    return {m.name: m.name in online for m in machines}


def _fetch_pre_started_at(machines: list) -> dict[str, float | None]:
    """Capture each agent's `agent_started_at` BEFORE we trigger /update.

    Returns ``{name: started_at_or_None}`` — None when the agent is
    unreachable or doesn't expose the field yet.
    """
    out: dict[str, float | None] = {}
    for m in machines:
        try:
            resp = httpx.get(f"http://{m.host}:{AGENT_PORT}/health", timeout=3.0)
            if resp.status_code == 200:
                out[m.name] = resp.json().get("agent_started_at")
            else:
                out[m.name] = None
        except Exception:
            out[m.name] = None
    return out


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

    hostname = socket.gethostname().lower()
    short = hostname.split(".")[0]
    candidates = [m for m in cfg.machines if m.name.lower() == short or m.host.lower() == hostname or m.host.lower().split(".")[0] == short]
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


@click.command(
    help=(
        "Pause a machine — no new agents will be routed to it until "
        "`coord unpause` is called.  In-flight assignments are NOT "
        "cancelled (use `coord stop` for that).\n\n"
        "MACHINE is the local name from coordinator.yml."
    ),
)


@_CONFIG_OPTION
@click.argument("machine")
def pause(config_path: Path, machine: str) -> None:
    from coord.machine_pause import pause as _pause
    changed = _pause(machine)
    if changed:
        click.echo(f"paused: {machine}")
    else:
        click.echo(f"already paused: {machine}")


@click.command(
    help=(
        "Resume a paused machine — new assignments can be routed to it "
        "again.  No-op if the machine wasn't paused."
    ),
)


@_CONFIG_OPTION
@click.argument("machine")
def unpause(config_path: Path, machine: str) -> None:
    from coord.machine_pause import unpause as _unpause
    changed = _unpause(machine)
    if changed:
        click.echo(f"resumed: {machine}")
    else:
        click.echo(f"not paused: {machine}")