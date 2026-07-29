"""The `coord agent` group: per-machine agent server lifecycle
(start/update/restart/clean-worktrees) plus `pause`/`unpause`.
Extracted from coord/cli.py (#747)."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import click
import httpx

from coord import __version__
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
        "POST /update to one or all agent servers, pinning the upgrade to "
        "this coordinator's own version (git pull for editable installs, "
        "pip install --no-cache-dir --upgrade claude-coordinator==<version> "
        "otherwise).  Polls each agent's self-reported version for up to "
        "--timeout seconds and reports success only once it matches the "
        "requested version, escalating to a `systemctl --user restart "
        "coord-agent` if the version is stuck."
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
    # #1568: the coordinator's own version IS the requested version — the
    # whole point of `coord agent update` is to bring the fleet in line
    # with whatever's running here.  Sending it lets the agent pin its pip
    # install to that exact release (turning a stale-index no-op into a
    # loud pip failure) and lets THIS command verify success by polling
    # for that exact version, instead of inferring success from "the POST
    # was accepted" (false positive on a cache-stale no-op) or failure
    # from "the process stopped answering pings" (false negative on the
    # execv-under-systemd restart, #404).
    target_version = __version__

    cfg = _load_config(config_path)
    targets = _resolve_agent_targets(cfg, machine_filter, all_machines)
    if not targets:
        click.echo("No machines to update.", err=True)
        sys.exit(2)

    click.echo(f"Requesting upgrade to v{target_version}...")

    # Capture each agent's start time BEFORE we trigger /update so the
    # wait loop can distinguish "old agent still answering during pip"
    # from "new agent came back up".
    pre_started_at = _fetch_pre_started_at(targets)

    for machine in targets:
        url = f"http://{machine.host}:{AGENT_PORT}/update"
        click.echo(f"  {machine.name}: POST {url} ...", nl=False)
        try:
            resp = httpx.post(
                url, json={"target_version": target_version}, timeout=10
            )
            if resp.status_code == 202:
                data = resp.json()
                click.echo(f" accepted (mode: {data.get('mode', '?')})")
            else:
                click.echo(f" HTTP {resp.status_code}")
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            click.echo(f" error: {e}")

    if targets:
        click.echo(
            f"\nWaiting up to {timeout}s for agent(s) to report v{target_version}..."
        )
        outcomes = _wait_agents_updated(
            targets,
            target_version=target_version,
            timeout=timeout,
            pre_started_at=pre_started_at,
        )

        click.echo("")
        all_matched = True
        for machine in targets:
            outcome = outcomes[machine.name]
            version_now = outcome["version_now"]
            if outcome["matched"]:
                vbefore = outcome.get("version_before") or "?"
                click.echo(f"  {machine.name}: ✓ {vbefore} → {target_version}")
                continue

            all_matched = False
            result = outcome.get("result")
            if result == "no_change":
                click.echo(
                    f"  {machine.name}: ✗ no change (still {version_now}) — "
                    f"{outcome.get('error') or 'pip resolved to the same version'}",
                    err=True,
                )
            elif result == "failed":
                err = outcome.get("error") or "pip failed; see ~/.coord/last_update.log"
                click.echo(f"  {machine.name}: ✗ failed — {err}", err=True)
            elif outcome.get("escalated"):
                click.echo(
                    f"  {machine.name}: ✗ pip upgraded to {target_version} but the "
                    f"process is stuck reporting {version_now} even after a "
                    "`systemctl --user restart` — needs manual investigation",
                    err=True,
                )
            elif not outcome.get("came_online"):
                click.echo(f"  {machine.name}: ✗ did not come back online", err=True)
            else:
                click.echo(
                    f"  {machine.name}: ✗ still reporting {version_now}, "
                    f"expected {target_version}",
                    err=True,
                )

        if not all_matched:
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
                # #1402: the same endpoint GCs the shared cargo target cache.
                cargo_mb = data.get("cargo_cache_bytes", 0) / (1024 * 1024)
                evicted = data.get("cargo_caches_evicted", 0)
                click.echo(
                    f" cleaned={cleaned} kept={kept} freed={freed_mb:.1f} MB "
                    f"cargo-cache={cargo_mb:.1f} MB (evicted {evicted})"
                )
            else:
                click.echo(f" HTTP {resp.status_code}")
                any_error = True
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            click.echo(f" error: {e}")
            any_error = True

    if any_error:
        sys.exit(1)


@agent.command(
    "versions",
    help=(
        "GET /health from one or all agent servers and print each one's "
        "self-reported version alongside the coordinator's own.  #1568: "
        "a version split-brain across the fleet is only detectable by "
        "comparing versions directly — this is the fleet-wide check to "
        "run before trusting a rule change, and after `coord agent "
        "update` to confirm it actually landed everywhere."
    ),
)
@_CONFIG_OPTION
@click.option(
    "--machine",
    "machine_filter",
    default=None,
    help="Name of a single machine to check (from coordinator.yml).",
)
@click.option(
    "--all",
    "all_machines",
    is_flag=True,
    help="Check all machines (mutually exclusive with --machine).",
)
def agent_versions(
    config_path: Path,
    machine_filter: str | None,
    all_machines: bool,
) -> None:
    cfg = _load_config(config_path)
    targets = _resolve_agent_targets(cfg, machine_filter, all_machines)
    if not targets:
        click.echo("No machines to check.", err=True)
        sys.exit(2)

    click.echo(f"coordinator: v{__version__}\n")

    versions_seen: set[str] = set()
    any_offline = False
    any_mismatch = False
    for machine in targets:
        version: str | None
        try:
            resp = httpx.get(f"http://{machine.host}:{AGENT_PORT}/health", timeout=5)
            version = resp.json().get("version") if resp.status_code == 200 else None
        except (httpx.HTTPError, httpx.TimeoutException):
            version = None

        if version is None:
            click.echo(f"  {machine.name}: ✗ unreachable", err=True)
            any_offline = True
            continue

        versions_seen.add(version)
        mismatch = version != __version__
        any_mismatch = any_mismatch or mismatch
        marker = "  ⚠ mismatch" if mismatch else ""
        click.echo(f"  {machine.name}: v{version}{marker}")

    if len(versions_seen) > 1:
        click.echo(
            f"\n⚠ split-brain: {len(versions_seen)} distinct versions across the "
            f"fleet ({', '.join(sorted(versions_seen))}). Do not trust a rule "
            "change until `coord agent update --all` brings everyone in line.",
            err=True,
        )
        sys.exit(1)
    if any_mismatch:
        click.echo(
            f"\n⚠ mismatch: fleet is uniformly on a version that differs from "
            f"the coordinator's own v{__version__}. Run `coord agent update "
            "--all` to bring the fleet in line.",
            err=True,
        )
        sys.exit(1)
    if any_offline:
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
    # Scale the sleep down for short timeouts (e.g. tests passing
    # --timeout 1) so a tiny deadline isn't dominated by a single fixed
    # 2s sleep — callers that want the full 2s just pass a bigger timeout.
    poll_interval = min(poll_interval, max(timeout / 5, 0.05))
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


def _wait_agents_updated(
    machines: list,
    *,
    target_version: str,
    timeout: float = 120.0,
    poll_interval: float = 2.0,
    pre_started_at: dict[str, float | None] | None = None,
) -> dict[str, dict]:
    """Poll /health on each machine until its self-reported version equals
    ``target_version``, escalating to a driven restart if needed.

    #1568: success is judged by the version the agent actually reports —
    never by "the POST was accepted" and never by "the process answers
    pings again."  Those liveness signals fail in opposite directions:

    - Cause A: pip resolves to a cached/stale version and exits 0.  The
      POST is accepted, the process never restarts, but nothing changed —
      the old ``_wait_agents_online``-based check reported success anyway.
    - Cause B (#404): the update's ``os.execv`` self-restart doesn't take
      under systemd, so the OLD process keeps answering /health after a
      real upgrade.  ``_wait_agents_online`` reported "did not come back"
      even though the new version was installed and the service was
      active.

    When a machine's pip step genuinely succeeded (``last_update.result
    == "upgraded"``) but the version still hasn't advanced once the
    normal poll window elapses, escalate once with an SSH-driven
    ``systemctl --user restart coord-agent`` — the documented fix for the
    execv-under-systemd stall (see docs/AGENT_OPERATIONS.md) — and give
    it one more short window before giving up.

    Returns ``{machine_name: {matched, came_online, version_now,
    version_before, result, error, escalated}}``.
    """
    # Scale the sleep down for short timeouts (e.g. tests passing
    # --timeout 1) so a tiny deadline isn't dominated by a single fixed
    # 2s sleep — callers that want the full 2s just pass a bigger timeout.
    poll_interval = min(poll_interval, max(timeout / 5, 0.05))
    pre = pre_started_at or {}
    out: dict[str, dict] = {
        m.name: {
            "matched": False,
            "came_online": False,
            "version_now": "?",
            "version_before": None,
            "result": None,
            "error": None,
            "escalated": False,
        }
        for m in machines
    }
    pending = {m.name: m for m in machines}

    def _poll_once(machine) -> bool:
        """Fetch /health once, update out[machine.name], return True on match."""
        info = out[machine.name]
        try:
            resp = httpx.get(f"http://{machine.host}:{AGENT_PORT}/health", timeout=3.0)
            if resp.status_code != 200:
                return False
            health = resp.json()
        except Exception:
            return False

        version_now = health.get("version")
        info["version_now"] = version_now or "?"
        last = health.get("last_update") or {}
        info["result"] = last.get("result")
        info["error"] = last.get("error")
        info["version_before"] = last.get("version_before")

        if machine.name in pre:
            pre_val = pre[machine.name]
            cur = health.get("agent_started_at")
            if cur is None or pre_val is None or cur != pre_val:
                info["came_online"] = True
        else:
            info["came_online"] = True

        if version_now == target_version:
            info["matched"] = True
            return True
        return False

    deadline = time.time() + timeout
    while time.time() < deadline and pending:
        for name in list(pending):
            if _poll_once(pending[name]):
                del pending[name]
        if not pending:
            break
        time.sleep(poll_interval)

    # Escalate the machines still stuck on the old version whose pip step
    # actually succeeded — the classic execv-under-systemd stall.  Give
    # each a short follow-up window after the driven restart.
    escalate_timeout = min(30.0, max(timeout / 2, 15.0))
    for name in list(pending):
        machine = pending[name]
        info = out[name]
        if info["result"] != "upgraded":
            continue
        info["escalated"] = _escalate_restart(machine)
        if not info["escalated"]:
            continue
        sub_deadline = time.time() + escalate_timeout
        while time.time() < sub_deadline:
            if _poll_once(machine):
                del pending[name]
                break
            time.sleep(poll_interval)

    return out


def _escalate_restart(machine) -> bool:
    """Best-effort ``systemctl --user restart coord-agent`` over SSH.

    #404 / #1568: ``/update``'s ``os.execv`` self-restart does not take
    under systemd — same PID, stale version.  ``XDG_RUNTIME_DIR=/run/user/
    $(id -u)`` is load-bearing: a bare ``systemctl --user restart``
    silently no-ops in a non-interactive SSH session.  See
    docs/AGENT_OPERATIONS.md for the manual runbook this automates.

    Returns True if the ssh command itself exited 0 — NOT whether the
    agent actually came back on the new version; the caller re-polls
    /health afterwards to confirm that.
    """
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        machine.host,
        "XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart coord-agent",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except Exception:
        return False
    return result.returncode == 0


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