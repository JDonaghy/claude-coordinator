"""Click CLI entry point for the `coord` command."""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import click
import httpx

from coord import __version__, github_ops
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
    cwd = Path(os.getcwd())
    config_file = cwd / "coordinator.yml"

    # ── Step 1: Check for existing config ───────────────────────────────
    if config_file.exists():
        if not click.confirm(
            "coordinator.yml already exists. Overwrite?", default=False
        ):
            click.echo("Aborted.")
            return

    # ── Step 2: Detect current machine ──────────────────────────────────
    click.echo("\n── Machine setup ──")
    hostname = socket.gethostname()
    short_hostname = hostname.split(".")[0]
    machine_name = click.prompt("Machine name", default=short_hostname)

    detected_caps: list[str] = []
    # gtk: check via pkg-config
    try:
        subprocess.run(
            ["pkg-config", "--exists", "gtk4"],
            capture_output=True,
            check=True,
        )
        detected_caps.append("gtk")
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    # rust
    if shutil.which("cargo"):
        detected_caps.append("rust")
    # python
    if shutil.which("python3"):
        detected_caps.append("python")
    # docker
    if shutil.which("docker"):
        detected_caps.append("docker")
    # node
    if shutil.which("node"):
        detected_caps.append("node")

    if detected_caps:
        click.echo(f"Detected capabilities: {', '.join(detected_caps)}")
    else:
        click.echo("No capabilities auto-detected.")
    caps_input = click.prompt(
        "Capabilities (comma-separated)", default=",".join(detected_caps)
    )
    capabilities = [c.strip() for c in caps_input.split(",") if c.strip()]

    # ── Step 3: Discover repos ──────────────────────────────────────────
    click.echo("\n── Repo discovery ──")
    candidate_dirs: list[Path] = []
    # Scan cwd
    if (cwd / ".git").is_dir():
        candidate_dirs.append(cwd)
    # Scan ~/src/
    src_dir = Path.home() / "src"
    if src_dir.is_dir():
        for child in sorted(src_dir.iterdir()):
            if child.is_dir() and (child / ".git").is_dir():
                if child.resolve() != cwd.resolve():
                    candidate_dirs.append(child)

    # For each candidate, try to get the GitHub remote
    discovered: list[dict] = []
    for d in candidate_dirs:
        try:
            result = subprocess.run(
                ["git", "-C", str(d), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=True,
            )
            remote_url = result.stdout.strip()
            gh = _parse_github_remote(remote_url)
            if gh:
                repo_name = gh.split("/")[-1]
                discovered.append(
                    {"name": repo_name, "github": gh, "path": str(d)}
                )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    if not discovered:
        click.echo("No git repos with GitHub remotes found in cwd or ~/src/.")
        click.echo("You can edit coordinator.yml manually to add repos.")
        # Write a minimal config with just the machine
        yaml_str = _build_init_yaml(
            repos=[],
            machines=[
                {
                    "name": machine_name,
                    "host": hostname,
                    "capabilities": capabilities,
                    "repos": [],
                    "repo_paths": {},
                }
            ],
            max_workers=2,
            stagger_seconds=30,
        )
        config_file.write_text(yaml_str)
        click.echo(f"\nCreated coordinator.yml with 0 repos and 1 machine.")
        click.echo("Next: edit coordinator.yml to add repos, then run 'coord agent'.")
        return

    click.echo("Found repos:")
    for i, r in enumerate(discovered, 1):
        click.echo(f"  [{i}] {r['github']} ({r['path']})")

    selection = click.prompt(
        'Which repos to include? (comma-separated numbers or "all")',
        default="all",
    )
    if selection.strip().lower() == "all":
        selected_repos = list(discovered)
    else:
        try:
            indices = [int(x.strip()) for x in selection.split(",")]
            selected_repos = [discovered[i - 1] for i in indices if 1 <= i <= len(discovered)]
        except (ValueError, IndexError):
            click.echo("Invalid selection — including all repos.")
            selected_repos = list(discovered)

    if not selected_repos:
        click.echo("No repos selected.")
        selected_repos = []

    # Gather per-repo details
    repos_config: list[dict] = []
    repo_names = [r["name"] for r in selected_repos]
    for r in selected_repos:
        click.echo(f"\n  Configuring {r['name']} ({r['github']}):")

        # Detect default branch
        default_branch = "main"
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    r["path"],
                    "symbolic-ref",
                    "refs/remotes/origin/HEAD",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            ref = result.stdout.strip()  # e.g. refs/remotes/origin/main
            default_branch = ref.rsplit("/", 1)[-1]
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

        default_branch = click.prompt("    Default branch", default=default_branch)

        # Dependencies
        other_repos = [n for n in repo_names if n != r["name"]]
        if other_repos:
            deps_input = click.prompt(
                f"    Dependencies ({', '.join(other_repos)} or none)",
                default="none",
            )
            if deps_input.strip().lower() == "none":
                deps: list[str] = []
            else:
                deps = [d.strip() for d in deps_input.split(",") if d.strip()]
        else:
            deps = []

        build_cmd = click.prompt("    Build command (enter to skip)", default="", show_default=False)
        test_cmd = click.prompt("    Test command (enter to skip)", default="", show_default=False)

        repos_config.append(
            {
                "name": r["name"],
                "github": r["github"],
                "depends_on": deps,
                "default_branch": default_branch,
                "build_command": build_cmd or None,
                "test_command": test_cmd or None,
                "path": r["path"],
            }
        )

    # Build this machine's repo list and paths
    local_repo_names = [r["name"] for r in repos_config]
    local_repo_paths = {r["name"]: r["path"] for r in repos_config}

    machines_config: list[dict] = [
        {
            "name": machine_name,
            "host": hostname,
            "capabilities": capabilities,
            "repos": local_repo_names,
            "repo_paths": local_repo_paths,
        }
    ]

    # ── Step 4: Ask about other machines ────────────────────────────────
    click.echo("\n── Additional machines ──")
    while click.confirm("Add another machine?", default=False):
        m_name = click.prompt("  Machine name")
        m_host = click.prompt("  Tailscale hostname")
        m_caps_input = click.prompt("  Capabilities (comma-separated)", default="")
        m_caps = [c.strip() for c in m_caps_input.split(",") if c.strip()]

        click.echo(f"  Available repos: {', '.join(local_repo_names)}")
        m_repos_input = click.prompt(
            '  Which repos? (comma-separated names or "all")', default="all"
        )
        if m_repos_input.strip().lower() == "all":
            m_repos = list(local_repo_names)
        else:
            m_repos = [r.strip() for r in m_repos_input.split(",") if r.strip() in local_repo_names]

        m_repo_paths: dict[str, str] = {}
        for rn in m_repos:
            m_repo_paths[rn] = click.prompt(f"  Path to {rn} on {m_name}", default=f"~/src/{rn}")

        # Try to reach the machine
        try:
            resp = httpx.get(f"http://{m_host}:7433/health", timeout=3)
            click.echo(f"  ✓ {m_host} is reachable (HTTP {resp.status_code})")
        except Exception:
            click.echo(f"  ✗ {m_host} is not reachable (agent may not be running yet)")

        machines_config.append(
            {
                "name": m_name,
                "host": m_host,
                "capabilities": m_caps,
                "repos": m_repos,
                "repo_paths": m_repo_paths,
            }
        )

    # ── Step 5: Concurrency settings ────────────────────────────────────
    click.echo("\n── Concurrency settings ──")
    max_workers = click.prompt("Max concurrent workers", default=2, type=int)
    stagger_seconds = click.prompt(
        "Stagger seconds between dispatches", default=30, type=int
    )

    # ── Step 6: Generate coordinator.yml ────────────────────────────────
    yaml_str = _build_init_yaml(
        repos=repos_config,
        machines=machines_config,
        max_workers=max_workers,
        stagger_seconds=stagger_seconds,
    )
    config_file.write_text(yaml_str)

    # ── Step 7: Validate ────────────────────────────────────────────────
    try:
        from coord.config import load as load_config

        load_config(config_file)
    except Exception as e:
        click.echo(f"\nWarning: generated config has a validation error: {e}", err=True)
        click.echo("You may need to edit coordinator.yml manually.", err=True)
        return

    # ── Step 8: Print next steps ────────────────────────────────────────
    click.echo(
        f"\nCreated coordinator.yml with {len(repos_config)} repo(s) "
        f"and {len(machines_config)} machine(s)."
    )
    click.echo("Next: start the agent with 'coord agent', then run 'coord plan'.")


def _parse_github_remote(url: str) -> str | None:
    """Extract owner/repo from a GitHub remote URL.

    Handles both:
      git@github.com:owner/repo.git
      https://github.com/owner/repo.git
    """
    import re

    # SSH format
    m = re.match(r"git@github\.com:(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1)
    # HTTPS format
    m = re.match(r"https?://github\.com/(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1)
    return None


def _yaml_scalar(value: str) -> str:
    """Quote a YAML scalar if it contains special chars, otherwise return bare."""
    if not value:
        return '""'
    needs_quoting = any(c in value for c in ":#{}[]|>&*!%@`,?") or value != value.strip()
    if needs_quoting:
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _build_init_yaml(
    repos: list[dict],
    machines: list[dict],
    max_workers: int,
    stagger_seconds: int,
) -> str:
    """Build a coordinator.yml string with inline comments."""
    lines: list[str] = []

    # Repos
    lines.append("repos:")
    if not repos:
        lines.append("  # Add repos here. Example:")
        lines.append("  # - name: my-project")
        lines.append("  #   github: owner/my-project")
        lines.append("  #   default_branch: main")
        lines.append("  #   build_command: make build")
        lines.append("  #   test_command: make test")
        lines.append("  []")
    else:
        for r in repos:
            lines.append(f"  - name: {_yaml_scalar(r['name'])}")
            lines.append(f"    github: {_yaml_scalar(r['github'])}")
            deps = r.get("depends_on", [])
            if deps:
                deps_str = ", ".join(deps)
                lines.append(f"    depends_on: [{deps_str}]")
            else:
                lines.append("    depends_on: []")
            lines.append(f"    default_branch: {_yaml_scalar(r['default_branch'])}")
            if r.get("build_command"):
                lines.append(f"    build_command: {_yaml_scalar(r['build_command'])}")
            if r.get("test_command"):
                lines.append(f"    test_command: {_yaml_scalar(r['test_command'])}")
            lines.append("")

    lines.append("")
    lines.append("machines:")
    for m in machines:
        lines.append(f"  - name: {_yaml_scalar(m['name'])}")
        lines.append(f"    host: {_yaml_scalar(m['host'])}")
        caps = m.get("capabilities", [])
        if caps:
            caps_str = ", ".join(caps)
            lines.append(f"    capabilities: [{caps_str}]")
        else:
            lines.append("    capabilities: []")
        mrepos = m.get("repos", [])
        if mrepos:
            repos_str = ", ".join(mrepos)
            lines.append(f"    repos: [{repos_str}]")
        else:
            lines.append("    repos: []")
        rpaths = m.get("repo_paths", {})
        if rpaths:
            lines.append("    repo_paths:")
            for rn, rp in rpaths.items():
                lines.append(f"      {rn}: {_yaml_scalar(rp)}")
        lines.append("")

    lines.append("")
    lines.append("# Concurrency settings")
    lines.append("concurrency:")
    lines.append(f"  max_workers: {max_workers}          # max simultaneous claude -p sessions")
    lines.append(f"  stagger_seconds: {stagger_seconds}     # delay between starting workers")
    lines.append("")
    lines.append("# Lifecycle hooks (optional)")
    lines.append("# hooks:")
    lines.append("#   on_round_complete:")
    lines.append("#     - summary_report")
    lines.append("#   on_session_end:")
    lines.append("#     - summary_report")
    lines.append("")

    return "\n".join(lines) + "\n"


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
        repo_paths=machine.repo_paths,
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


@main.command(help="Show all machines, assignments, and connectivity.")
@_CONFIG_OPTION
@click.option("--machine", "machine_filter", default=None, help="Only show this machine.")
@click.option("--timeout", default=3.0, show_default=True, type=float, help="Per-machine health-check timeout (seconds).")
@click.option(
    "--freshness",
    is_flag=True,
    help="Also report per-machine repo freshness vs GitHub HEADs.",
)
def status(config_path: Path, machine_filter: str | None, timeout: float, freshness: bool) -> None:
    from coord import freshness as fresh
    from coord.deps import blocked_repos as compute_blocked, build_dep_graph
    from coord.network import check_all, fetch_repos, fetch_status
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
                spec_type = spec.get("type", "work")
                badge_map = {"review": "[review] ", "smoke": "[smoke] "}
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

    queue = mq.load_queue()
    by_repo = mq.pending_summary(queue)
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
                if e.error:
                    click.echo(f"      error: {e.error}")

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
    click.echo("Gathering context...", nl=False)
    sys.stdout.flush()

    from coord.brain import gather_context, build_prompt, call_claude, parse_proposals, parse_split_proposals, SYSTEM_PROMPT
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


@main.command(help="Dispatch approved assignments (comma-separated IDs).")
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
    from coord.deps import blocked_repos as compute_blocked, build_dep_graph, transitive_deps
    from coord.dispatch import compute_do_not_touch, dispatch, dispatch_with_retry, post_briefing
    from coord.network import classify_error, fetch_repos
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

    for p in selected:
        click.echo(f"[{p.id}] {p.machine_name} → {p.repo_name} #{p.issue_number}: {p.issue_title}")
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
            record_dispatched(assignment_id=assignment_id, proposal=p, repo_github=repo.github)

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
        board = build_board()
        board.round_number += 1
        save_board(board)
        click.echo("\nPending proposals cleared. Board saved.")


@main.command(help="Directly assign an issue to a machine, bypassing coord plan.")
@click.argument("machine")
@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
@click.option("--briefing", default="", help="Optional briefing text for the worker.")
@click.option("--dry-run", is_flag=True, help="Show what would be dispatched.")
def assign(
    machine: str,
    repo: str,
    issue: int,
    config_path: Path,
    briefing: str,
    dry_run: bool,
) -> None:
    from coord.dispatch import dispatch, post_briefing
    from coord.state import build_board, load_dispatched, record_dispatched, save_board

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

    # Fetch the issue title from GitHub
    try:
        issue_data = github_ops.get_issue(repo_cfg.github, issue)
    except RuntimeError as e:
        click.echo(f"error: could not fetch issue #{issue}: {e}", err=True)
        sys.exit(1)
    issue_title = issue_data.get("title", f"Issue #{issue}")

    # Build a Proposal inline
    from coord.models import Proposal

    proposal = Proposal(
        id=0,
        machine_name=machine,
        repo_name=repo,
        issue_number=issue,
        issue_title=issue_title,
        rationale="manual assignment via coord assign",
        briefing=briefing,
    )

    click.echo(f"{machine} → {repo} #{issue}: {issue_title}")

    if dry_run:
        click.echo("  (dry run — not dispatched)")
        return

    # Claim check
    from coord.claim import claim_message, find_work_claim

    board = build_board()
    claim = find_work_claim(issue, repo, repo_cfg.github, board)
    if claim is not None:
        click.echo(
            f"  skipping: {claim_message(claim)}",
            err=True,
        )
        sys.exit(1)

    # Dispatch to agent server
    try:
        response = dispatch(proposal, cfg)
    except httpx.HTTPError as e:
        click.echo(f"  dispatch failed: {e}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"  dispatch failed: {e}", err=True)
        sys.exit(1)

    assignment_id = response.get("id", "pending")
    click.echo(f"  dispatched (assignment {assignment_id})")

    # Record the dispatch
    record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github=repo_cfg.github,
    )

    # Post briefing to GitHub
    in_flight = load_dispatched()
    try:
        from coord.dispatch import compute_do_not_touch

        do_not_touch = compute_do_not_touch(proposal, peers=[], in_flight=in_flight)
        post_briefing(proposal, cfg, assignment_id=assignment_id, do_not_touch=do_not_touch)
        click.echo("  briefing posted to GitHub")
    except Exception as e:
        click.echo(f"  briefing post failed: {e}", err=True)

    # Update board
    board = build_board()
    save_board(board)


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


@main.command("test", help="Queue a smoke test for a completed assignment.")
@click.argument("assignment_id")
@_CONFIG_OPTION
def test_cmd(assignment_id: str, config_path: Path) -> None:
    from coord.smoke import dispatch_smoke
    from coord.state import build_board, load_board, save_board

    cfg = _load_config(config_path)
    board = load_board() or build_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)
    if assignment.status != "done":
        click.echo(
            f"error: assignment {assignment_id} is {assignment.status!r}, "
            "smoke can only run on done work assignments",
            err=True,
        )
        sys.exit(1)
    if assignment.type != "work":
        click.echo(
            f"error: assignment {assignment_id} is type {assignment.type!r}; "
            "only 'work' assignments get smoke tests",
            err=True,
        )
        sys.exit(1)

    from coord.claim import has_active_followup

    if has_active_followup(
        board, of_assignment_id=assignment_id, assignment_type="smoke"
    ):
        click.echo(
            f"error: a smoke test for assignment {assignment_id} is already "
            "running. Use `coord status` to see it.",
            err=True,
        )
        sys.exit(1)

    cfg.smoke_tests.auto_queue = True
    smoke = dispatch_smoke(assignment, board, cfg)
    if smoke is None:
        click.echo(
            "No smoke test was queued. Possible reasons: no matching "
            "capability_rules, no capable machine, or HTTP failure reaching "
            "the agent.",
            err=True,
        )
        sys.exit(1)

    save_board(board)
    click.echo(
        f"Smoke test {smoke.assignment_id} queued on {smoke.machine_name} "
        f"for branch {smoke.branch}"
    )


@main.command(help="Re-dispatch a failed assignment to a different machine.")
@click.argument("assignment_id")
@_CONFIG_OPTION
def retry(assignment_id: str, config_path: Path) -> None:
    from coord.reconcile import _reassign
    from coord.state import build_board, load_board, save_board

    cfg = _load_config(config_path)
    board = load_board() or build_board()

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

    result = _reassign(assignment, board, cfg)
    if result is None:
        click.echo("error: no available machine to retry on", err=True)
        sys.exit(1)

    save_board(board)
    click.echo(
        f"Retried: {result.machine_name} → {result.repo_name} "
        f"#{result.issue_number} (assignment {result.assignment_id})"
    )


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


@main.command(help="Process the merge queue: open PRs and merge in sequence.")
@_CONFIG_OPTION
@click.option("--dry-run", is_flag=True, help="Show the plan without opening or merging PRs.")
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
def merge(
    config_path: Path,
    dry_run: bool,
    order: str | None,
    repo_filter: str | None,
    method: str,
) -> None:
    from coord import github_ops as gh_ops
    from coord import merge_queue as mq
    from coord.merge_queue import CONFLICT, MERGED, PENDING

    _load_config(config_path)  # validate
    items = mq.load_queue()
    if repo_filter:
        items = [x for x in items if x.repo_name == repo_filter]
    if not items:
        click.echo("Merge queue is empty.")
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

    events = mq.process(items, gh_ops, method=method, dry_run=dry_run, presorted=presorted)

    for ev in events:
        e = ev.entry
        prefix = f"  {e.repo_name} #{e.issue_number} ({e.branch})"
        click.echo(f"{prefix}: {ev.kind} — {ev.message}")

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
@click.option("--output", "output_file", type=click.Path(), default=None,
              help="File with test output to store (used with --fail).")
def test(assignment_id: str, config_path: Path, verdict: str | None, reason: str, output_file: str | None) -> None:
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

        # Store test output when --fail --output is provided
        if verdict == "fail" and output_file:
            output_path = Path(output_file)
            if output_path.exists():
                from coord.state import COORD_DIR

                test_output_dir = COORD_DIR / "test_output"
                test_output_dir.mkdir(parents=True, exist_ok=True)
                stored = test_output_dir / f"{assignment_id}.txt"
                stored.write_text(output_path.read_text())
                # Record the stored path so coord fix can find it
                assignment.smoke_test_reason = (
                    f"{reason} [output: {stored}]" if reason else f"[output: {stored}]"
                )
                click.echo(f"  test output stored: {stored}")
            else:
                click.echo(f"  warning: output file not found: {output_file}", err=True)

        save_board(board)
        if verdict == "pass":
            click.echo(f"Smoke test PASSED for {assignment.repo_name} #{assignment.issue_number}")
            click.echo(f"  Run: coord pr {assignment_id} to create the PR")
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


@main.command(help="Block until an assignment completes (poll the agent server).")
@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option("--interval", default=30, show_default=True, type=int, help="Seconds between polls.")
@click.option("--timeout", default=1800, show_default=True, type=int, help="Max seconds to wait.")
def wait(assignment_id: str, config_path: Path, interval: int, timeout: int) -> None:
    from coord.state import load_dispatched

    cfg = _load_config(config_path)

    # Find which machine this assignment was dispatched to
    record = next(
        (r for r in load_dispatched() if r.get("assignment_id") == assignment_id),
        None,
    )
    if record is None:
        click.echo(f"error: assignment {assignment_id!r} not found in dispatched records", err=True)
        sys.exit(2)

    machine_name = record["machine_name"]
    machine = next((m for m in cfg.machines if m.name == machine_name), None)
    if machine is None:
        click.echo(
            f"error: machine {machine_name!r} (from dispatched record) not in coordinator.yml",
            err=True,
        )
        sys.exit(2)

    url = f"http://{machine.host}:{AGENT_PORT}/status"
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=10)
            data = resp.json()
        except (httpx.HTTPError, httpx.TimeoutException, OSError) as e:
            click.echo(f"warning: could not reach agent on {machine.name}: {e}", err=True)
            time.sleep(interval)
            continue

        # Check completed list
        for c in data.get("completed", []):
            if c.get("id") == assignment_id:
                exit_code = c.get("exit_code", -1)
                branch = c.get("branch", "unknown")
                started = c.get("started_at", 0)
                finished = c.get("finished_at", 0)
                duration = finished - started if finished and started else 0
                mins, secs = divmod(int(duration), 60)

                if exit_code == 0:
                    click.echo(f"Assignment {assignment_id} completed (exit 0, {mins}m {secs}s)")
                    click.echo(f"  branch: {branch}")
                    sys.exit(0)
                else:
                    click.echo(f"Assignment {assignment_id} failed (exit {exit_code}, {mins}m {secs}s)")
                    error = c.get("error", "")
                    if error:
                        click.echo(f"  error: {error}")
                    click.echo(f"  branch: {branch}")
                    sys.exit(1)

        # Check active list — if not there either, it vanished
        active_ids = [a.get("id") for a in data.get("active", [])]
        if assignment_id not in active_ids:
            click.echo(
                f"Assignment {assignment_id} not found on agent (not active or completed)",
                err=True,
            )
            sys.exit(2)

        time.sleep(interval)

    # Timeout
    click.echo(f"Timed out after {timeout}s waiting for {assignment_id}", err=True)
    sys.exit(3)


def _dispatch_followup(
    cfg: Config,
    original: Assignment,
    briefing: str,
    *,
    issue_suffix: str = "",
) -> str:
    """Dispatch a follow-up assignment for an existing assignment. Returns assignment ID."""
    from coord.dispatch import dispatch, post_briefing, compute_do_not_touch
    from coord.state import build_board, record_dispatched, save_board, load_dispatched
    from coord.models import Proposal

    repo = cfg.repo(original.repo_name)
    if repo is None:
        raise ValueError(f"Unknown repo: {original.repo_name!r}")

    proposal = Proposal(
        id=0,
        machine_name=original.machine_name,
        repo_name=original.repo_name,
        issue_number=original.issue_number,
        issue_title=original.issue_title,
        rationale=f"follow-up for assignment {original.assignment_id}",
        briefing=briefing,
    )

    response = dispatch(proposal, cfg)
    assignment_id = response.get("id", "pending")
    record_dispatched(assignment_id=assignment_id, proposal=proposal, repo_github=repo.github)

    in_flight = load_dispatched()
    do_not_touch = compute_do_not_touch(proposal, peers=[], in_flight=in_flight)
    post_briefing(proposal, cfg, assignment_id=assignment_id, do_not_touch=do_not_touch)

    # Update board
    board = build_board()
    save_board(board)

    return assignment_id


@main.command(help="Dispatch a worker to create a PR for a completed assignment.")
@click.argument("assignment_id")
@_CONFIG_OPTION
def pr(assignment_id: str, config_path: Path) -> None:
    from coord.state import build_board, load_board

    cfg = _load_config(config_path)
    board = load_board() or build_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    if assignment.status != "done":
        click.echo(
            f"error: assignment {assignment_id} is {assignment.status!r}, "
            "can only create a PR for done assignments",
            err=True,
        )
        sys.exit(1)

    if not assignment.branch:
        click.echo(
            f"error: assignment {assignment_id} has no branch recorded. "
            "The worker may not have pushed yet.",
            err=True,
        )
        sys.exit(1)

    repo = cfg.repo(assignment.repo_name)
    if repo is None:
        click.echo(f"error: unknown repo {assignment.repo_name!r}", err=True)
        sys.exit(1)

    default_branch = repo.default_branch
    briefing = (
        f"You are on branch {assignment.branch}. The code is complete and tests pass.\n"
        f"Create a PR from {assignment.branch} to {default_branch} for issue #{assignment.issue_number}.\n"
        f"Title: {assignment.issue_title}\n\n"
        f"Use gh pr create. Read the diff (git diff {default_branch}...HEAD) and write a clear\n"
        f"summary of what changed. Reference the issue with \"Closes #{assignment.issue_number}\".\n"
        f"Do NOT modify any code — only create the PR."
    )

    try:
        new_id = _dispatch_followup(cfg, assignment, briefing)
    except httpx.HTTPError as e:
        click.echo(f"error: dispatch failed: {e}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    click.echo(f"PR worker dispatched (assignment {new_id})")
    click.echo(f"  branch: {assignment.branch} → {default_branch}")
    click.echo(f"  issue: #{assignment.issue_number}: {assignment.issue_title}")


@main.command(help="Dispatch a fix-up worker for a failed smoke test.")
@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option("--guidance", default="", help="Additional guidance for the fix-up worker.")
def fix(assignment_id: str, config_path: Path, guidance: str) -> None:
    from coord.state import build_board, load_board, COORD_DIR

    cfg = _load_config(config_path)
    board = load_board() or build_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    if assignment.smoke_test != "fail":
        click.echo(
            f"error: assignment {assignment_id} smoke_test is "
            f"{assignment.smoke_test!r}, expected 'fail'",
            err=True,
        )
        sys.exit(1)

    repo = cfg.repo(assignment.repo_name)
    if repo is None:
        click.echo(f"error: unknown repo {assignment.repo_name!r}", err=True)
        sys.exit(1)

    default_branch = repo.default_branch

    # Load stored test output if available
    test_output = ""
    test_output_file = COORD_DIR / "test_output" / f"{assignment_id}.txt"
    if test_output_file.exists():
        test_output = test_output_file.read_text()
    elif assignment.smoke_test_reason:
        test_output = assignment.smoke_test_reason

    guidance_text = guidance or "Fix the failing tests and push."

    briefing = (
        f"You are fixing a failed smoke test for issue #{assignment.issue_number}: {assignment.issue_title}\n\n"
        f"The previous worker created branch {assignment.branch}. You are already on that branch.\n"
        f"Do NOT start over — work from the existing code.\n\n"
        f"## What was done\n"
        f"The previous worker's changes are already committed on this branch.\n"
        f"Run `git log --oneline {default_branch}..HEAD` to see what was done.\n"
        f"Run `git diff {default_branch}...HEAD` to see the full diff.\n\n"
        f"## Test failure\n"
        f"{test_output}\n\n"
        f"## Guidance\n"
        f"{guidance_text}\n\n"
        f"## Rules\n"
        f"- Do NOT start over or rewrite from scratch\n"
        f"- Fix the specific test failures\n"
        f"- Commit your fixes and push with git push origin HEAD"
    )

    try:
        new_id = _dispatch_followup(cfg, assignment, briefing)
    except httpx.HTTPError as e:
        click.echo(f"error: dispatch failed: {e}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    click.echo(f"Fix-up worker dispatched (assignment {new_id})")
    click.echo(f"  branch: {assignment.branch}")
    click.echo(f"  issue: #{assignment.issue_number}: {assignment.issue_title}")
    if test_output:
        click.echo(f"  test output included in briefing ({len(test_output)} chars)")


if __name__ == "__main__":
    main()
