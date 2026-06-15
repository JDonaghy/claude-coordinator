"""Click CLI entry point for the `coord` command."""

from __future__ import annotations

import json
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
# Portable control-center daemon port (#584); canonical constant in
# coord.serve_app.SERVE_PORT — duplicated here for the CLI decorator default,
# mirroring the AGENT_PORT pattern above.
SERVE_PORT = 7435


_CONFIG_OPTION = click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=DEFAULT_CONFIG_PATH,
    show_default=True,
    help="Path to coordinator.yml.",
)


def _save_config_snapshot(config: Config) -> None:
    """Persist machine + pipeline metadata to the DB so dashboards can read it.

    Writes:
    - ``machines`` rows (used by the web dashboard + the TUI Machines view)
    - ``board_meta['pipeline_default_gates']`` JSON list of default gates
    - ``board_meta['pipeline_tracked_labels']`` JSON list of tracked GitHub
      issue labels (defaults to ``['coord']`` when unconfigured)

    The pipeline keys let the TUI Pipeline panel pick up coordinator.yml
    settings without having to parse YAML itself.
    """
    # #584: a thin client (board_service configured) must not create/write a
    # local DB — the daemon/host owns the config snapshot.  On the host
    # board_service is unset, so the snapshot is written as before.
    from coord.client import resolve_board_service
    if resolve_board_service() is not None:
        return
    conn = None
    try:
        from coord.db import get_connection
        conn = get_connection()
        conn.execute("DELETE FROM machines")
        for m in config.machines:
            conn.execute(
                "INSERT INTO machines (name, host, capabilities, repos) VALUES (?, ?, ?, ?)",
                (m.name, m.host, json.dumps(m.capabilities), json.dumps(m.repos)),
            )
        conn.execute(
            "INSERT OR REPLACE INTO board_meta (key, value) VALUES "
            "('pipeline_default_gates', ?)",
            (json.dumps(list(config.pipeline.default_gates)),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO board_meta (key, value) VALUES "
            "('pipeline_tracked_labels', ?)",
            (json.dumps(config.pipeline.tracked_labels()),),
        )
        # Repo name → GitHub slug map: the TUI pipeline panel uses this to
        # translate a `gh search issues` repository.nameWithOwner back into
        # the coord-local repo name expected by `coord assign`.
        conn.execute(
            "INSERT OR REPLACE INTO board_meta (key, value) VALUES "
            "('pipeline_repos', ?)",
            (json.dumps({r.name: r.github for r in config.repos}),),
        )
        # #296: run_cmd per repo — TUI surfaces this in the Test stage
        # detail panel as the "Run" row so the tester knows what to launch.
        # Only repos that have a run_cmd are included; absent → no entry.
        conn.execute(
            "INSERT OR REPLACE INTO board_meta (key, value) VALUES "
            "('pipeline_repo_run_cmds', ?)",
            (json.dumps({r.name: r.run_cmd for r in config.repos if r.run_cmd is not None}),),
        )
        # Whether the pipeline includes a Plan gate before Work. Sourced
        # from dispatch.require_plan — when true, the TUI prepends a Plan
        # stage and Work [Go] becomes "approve plan" rather than fresh
        # dispatch.
        conn.execute(
            "INSERT OR REPLACE INTO board_meta (key, value) VALUES "
            "('pipeline_require_plan', ?)",
            ("1" if config.dispatch.require_plan else "0",),
        )
        # #349: repo_name → local-checkout path for the machine running this
        # coordinator.  Used by the TUI to read git branch HEADs when
        # detecting test-plan staleness.  Only includes repos that have a
        # repo_paths entry on the matching machine (hostname-matched first;
        # any machine as fallback).
        local_hostname = socket.gethostname().split(".")[0]
        repo_paths_map: dict[str, str] = {}
        # Try hostname-matched machine first, then fall back to all machines.
        for pass_no in range(2):
            for m in config.machines:
                on_this_machine = (
                    m.name == local_hostname
                    or m.host.split(".")[0] == local_hostname
                )
                if pass_no == 0 and not on_this_machine:
                    continue
                for rn in m.repos:
                    if rn not in repo_paths_map:
                        p = m.repo_path(rn)
                        if p:
                            repo_paths_map[rn] = str(Path(p).expanduser())
        conn.execute(
            "INSERT OR REPLACE INTO board_meta (key, value) VALUES "
            "('pipeline_repo_paths', ?)",
            (json.dumps(repo_paths_map),),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 — non-critical, don't abort CLI
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass


def _load_config(path: Path) -> Config:
    # #584/#591: a thin client (board_service set) has no local coordinator.yml.
    # When the path is absent, fetch + cache the daemon's config and load that,
    # so EVERY command — not just `coord status` — is config-portable. On the
    # host (svc unset) or when a local config exists, this is a no-op.
    try:
        if not Path(path).exists():
            from coord.client import resolve_board_service  # noqa: PLC0415

            svc = resolve_board_service()
            if svc is not None:
                from coord.client import fetch_remote_config  # noqa: PLC0415

                try:
                    path = fetch_remote_config(svc)
                except Exception as exc:  # noqa: BLE001 — fall through to the normal error
                    click.echo(
                        f"warning: could not fetch config from {svc.url}: {exc}",
                        err=True,
                    )
        cfg = load(path)
    except ConfigError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(2)
    _save_config_snapshot(cfg)
    return cfg


def _get_assignment_branch_head(
    assignment_id: str,
    config: "Config",
    repo_path_fn: "Callable[[str, Config], Path | None]",
) -> str | None:
    """#349: Resolve the current HEAD SHA for an assignment's branch.

    Looks up the assignment's repo_name + branch from the DB, finds the local
    repo path via *repo_path_fn* (typically
    ``coord.test_orchestrator.find_local_repo_path``), then runs
    ``git rev-parse <branch>`` to get the SHA.

    Returns ``None`` when the assignment is not found, has no branch set, the
    local repo path can't be resolved, or git fails.  The caller treats ``None``
    as "HEAD unknown — skip staleness tracking".
    """
    import subprocess  # noqa: PLC0415 — lazy import keeps startup fast
    from coord.db import get_connection  # noqa: PLC0415

    conn = get_connection()
    row = conn.execute(
        "SELECT repo_name, branch FROM assignments WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()
    if not row:
        return None
    repo_name: str = row["repo_name"] if hasattr(row, "keys") else row[0]
    branch: str = (row["branch"] if hasattr(row, "keys") else row[1]) or ""
    if not branch:
        return None
    local_path = repo_path_fn(repo_name, config)
    if not local_path or not local_path.exists():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", branch],
            cwd=str(local_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def _warn_if_source_install_drift() -> None:
    """Warn when the CLI is running from a non-editable install of a package
    whose source checkout is the current working directory.

    Root cause of #222: ``pip install .`` (without ``-e``) copies a snapshot
    into site-packages. Subsequent edits in the source tree don't reach the
    CLI, while ``python -c "from coord.... import ..."`` from the source dir
    DOES pick them up (cwd shadows site-packages on import). Result: the same
    workflow gives different answers depending on entry path.

    Heuristic: ``coord.__file__`` lives in ``site-packages`` AND the cwd has
    a sibling ``coord/`` package — that's exactly the drift case.
    """
    import os  # noqa: PLC0415

    try:
        import coord as _coord  # noqa: PLC0415

        coord_file = _coord.__file__ or ""
        if "site-packages" not in coord_file:
            return  # Editable install — source IS the import path, no drift.
        local_init = Path(os.getcwd()) / "coord" / "__init__.py"
        if not local_init.exists():
            return  # Not running from a source checkout.
        # Inside a source checkout but CLI uses snapshot copy → drift possible.
        click.echo(
            "warning: coord CLI is running from a non-editable install "
            "(site-packages snapshot) but a source checkout exists at "
            f"{local_init.parent}.\n"
            "         Edits to the source tree will NOT reach the CLI.  "
            "Fix:  pip install -e .",
            err=True,
        )
    except Exception:  # noqa: BLE001 — best-effort, never break the CLI
        pass


def _warn_if_editable_checkout_moved() -> None:
    """#561/#601 backstop: when running from an EDITABLE checkout, warn loudly if
    its branch was moved off the default.

    A Build/`coord test`/smoke that git-checkout'd the base — or an interactive
    agent inspecting a branch in the live checkout — silently puts the running
    coordinator on that branch's code until restored (#561 incident: disabled
    guards; #601 incident: old code + retired local DB). This makes that state
    visible on every command instead of waiting for a verdict or manual restore.
    """
    import subprocess  # noqa: PLC0415
    import sys as _sys  # noqa: PLC0415

    if "pytest" in _sys.modules:
        return  # don't add startup noise to the test suite
    try:
        import coord as _coord  # noqa: PLC0415

        coord_file = _coord.__file__ or ""
        if "site-packages" in coord_file:
            return  # PyPI/snapshot install — moving a checkout can't affect it.
        repo_root = Path(coord_file).resolve().parents[1]
        if not (repo_root / ".git").exists():
            return
        head = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=3,
        )
        if head.returncode != 0:
            return
        branch = head.stdout.strip()
        if branch in ("main", "master"):
            return
        shown = "(detached HEAD)" if branch == "HEAD" else f"'{branch}'"
        click.echo(
            f"⚠ coord: editable checkout {repo_root} is on {shown}, not the "
            "default branch — the running coordinator is on THAT code. A "
            "Build/smoke/test may have checked it out. Restore with:  "
            f"git -C {repo_root} checkout main",
            err=True,
        )
    except Exception:  # noqa: BLE001 — best-effort, never break the CLI
        pass


@click.group(help="Multi-agent coordinator for Claude Code workers.")
@click.version_option(__version__, prog_name="coord")
def main() -> None:
    """coord — coordinate Claude Code workers across machines and repos."""
    _warn_if_source_install_drift()
    _warn_if_editable_checkout_moved()


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


def _ensure_coord_permissions(cwd: Path) -> None:
    """Check .claude/settings.local.json for Bash(coord *) / Bash(coord) entries.

    If either is absent, prompt the user and add both.  Skips silently when
    both entries are already present.
    """
    settings_dir = cwd / ".claude"
    settings_path = settings_dir / "settings.local.json"

    COORD_PERMS = ["Bash(coord *)", "Bash(coord)"]

    # Read existing settings or start fresh.
    data: dict = {}
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}

    existing_allow: list = data.get("permissions", {}).get("allow", [])
    missing = [p for p in COORD_PERMS if p not in existing_allow]

    if not missing:
        return  # Already configured — nothing to do.

    click.echo("\n── Claude Code permissions ──")
    click.echo(
        "  .claude/settings.local.json is missing: "
        + ", ".join(missing)
    )
    if click.confirm(
        "  Add Bash(coord *) and Bash(coord) to allow list?",
        default=True,
    ):
        if "permissions" not in data:
            data["permissions"] = {}
        if "allow" not in data["permissions"]:
            data["permissions"]["allow"] = []
        for perm in missing:
            if perm not in data["permissions"]["allow"]:
                data["permissions"]["allow"].append(perm)

        settings_dir.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(data, indent=2) + "\n")
        click.echo(f"  Updated: {settings_path}")
    else:
        click.echo(
            "  Skipped. Add them manually to .claude/settings.local.json."
        )


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
        _ensure_coord_permissions(cwd)
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

    # ── Step 9: Ensure Claude Code permissions are configured ────────────
    _ensure_coord_permissions(cwd)


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


@main.group(
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
@click.option("--no-reconcile", is_flag=True, help="Skip auto-reconciliation of the board with live agent state.")
@click.option(
    "--freshness",
    is_flag=True,
    help="Also report per-machine repo freshness vs GitHub HEADs.",
)
def status(config_path: Path, machine_filter: str | None, no_reconcile: bool, timeout: float, freshness: bool) -> None:
    from coord import freshness as fresh
    from coord.deps import blocked_repos as compute_blocked, build_dep_graph
    from coord.client import fetch_remote_board, fetch_remote_config, resolve_board_service
    from coord.network import check_all, fetch_repos, fetch_status
    from coord.state import build_board, load_board, load_dispatched, load_notified, save_board

    # #584: when a board service is configured, read the board + config from the
    # daemon instead of local SQLite.  Unset ⇒ unchanged local behaviour.
    svc = resolve_board_service()
    if svc and not Path(config_path).exists():
        config_path = fetch_remote_config(svc)
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
    board = fetch_remote_board(svc) if svc else (load_board() or build_board())
    if not no_reconcile and agent_completed and not svc:
        # Remote board service owns reconciliation + writes (#590); a thin client
        # must not write to a local DB.
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
            reconciled += 1
        if reconciled:
            save_board(board)
            click.echo(f"\n  (reconciled {reconciled} assignment(s) from live agent data)")

    # #448: surface advisory assignments (0 commits, clean exit) so the
    # operator knows they need attention without having to dig into logs.
    advisory_entries = [
        e for e in agent_completed.values()
        if e.get("status") == "advisory"
    ]
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
                if e.error:
                    click.echo(f"      error: {e.error}")

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

    # Show completed work assignments with review lifecycle state.
    _REVIEW_STATE_TAGS = {
        "pending": "[awaiting review]",
        "dispatched": "[review dispatched]",
        "done": "[review done]",
        "cap_hit": "[⚠ iteration cap hit — manual action required]",
    }
    work_completed = [a for a in board.completed if a.type == "work"]
    if work_completed:
        by_time = sorted(work_completed, key=lambda a: a.finished_at or 0, reverse=True)[:10]
        click.echo("")
        click.echo("Completed work assignments:")
        for a in by_time:
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


@main.command(help="Brain proposes assignments for idle machines.")
@_CONFIG_OPTION
@click.option("--dry-run", is_flag=True, help="Plan without saving proposals.")
def plan(config_path: Path, dry_run: bool) -> None:
    from coord.brain import propose
    from coord.state import save_proposals, save_split_proposals

    cfg = _load_config(config_path)
    click.echo("Gathering context...", nl=False)
    sys.stdout.flush()

    from coord.brain import gather_context, build_prompt, call_claude, parse_proposals, parse_split_proposals, resolve_required_gates, SYSTEM_PROMPT
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
        resolve_required_gates(proposals, cfg, context["issues_by_repo"])
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

    # ── Auto-split advisory ───────────────────────────────────────────────
    if cfg.dispatch.auto_split:
        from coord.split_work import analyze_plan, format_chunks_summary

        for p in selected:
            chunks = analyze_plan(p.files_likely, cfg.dispatch)
            if len(chunks) > 1:
                click.echo(
                    f"  ⚠ [{p.id}] {p.repo_name} #{p.issue_number} touches "
                    f"{len(p.files_likely)} files (threshold: "
                    f"{cfg.dispatch.max_files_per_worker}) — consider splitting:"
                )
                click.echo(format_chunks_summary(chunks))

    for p in selected:
        click.echo(f"[{p.id}] {p.machine_name} → {p.repo_name} #{p.issue_number}: {p.issue_title}")
        # Resolve model so the dispatched record and board reflect what ran.
        if not p.model:
            p.model = cfg.models.default
        # Resolve required_gates: fall back to config default for proposals
        # that were saved before label-based gate resolution was wired in.
        if not p.required_gates:
            p.required_gates = list(cfg.pipeline.default_gates)
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
            record_dispatched(
                assignment_id=assignment_id,
                proposal=p,
                repo_github=repo.github,
                provider_name=response.get("_provider_name"),
            )

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

        # Mark session start on first dispatch of the session
        from coord.state import load_session, write_session_start
        session = load_session()
        if session is None or session.get("clean_shutdown", True):
            write_session_start()


def _prompt_and_relay_review_verdict(
    *,
    assignment_id: str,
    repo_name: str,
    repo_github: str,
    issue_number: int,
    machine_name: str,
    verdict_cmd_hint: str,
) -> bool:
    """Prompt the operator for a review verdict on exit and relay it (#486d).

    Backstop used by BOTH interactive-review exit paths when the reviewer left
    without running `coord report-result` (local or remote — since #590 a
    remote `report-result` routes to the coordinator's shared DB via the daemon,
    so both paths *can* self-report; this prompt only fires when they didn't).

    Without it the verdict silently never reaches the merge gate and the
    Work→Review→Fix flow stalls.  Prompt the operator here (the terminal is a
    TTY) and relay through the same `issue_store` seam `coord report-result`
    uses — which itself routes to the daemon when `board_service` is set.

    No-op that prints the manual hint when stdin isn't a TTY (tests/headless).
    Returns True when a verdict was recorded.
    """
    if not sys.stdin.isatty():
        click.echo(f"  no verdict reported — record it with:\n{verdict_cmd_hint}")
        return False
    ans = click.prompt(
        "  Review verdict — [a]pprove / [r]equest-changes / [s]kip",
        type=click.Choice(["a", "r", "s"], case_sensitive=False),
        default="s",
        show_choices=True,
    )
    verdict = {"a": "approve", "r": "request-changes"}.get(ans.lower())
    if verdict is None:
        click.echo(f"  skipped — record the verdict later with:\n{verdict_cmd_hint}")
        return False
    summary = click.prompt(
        "  one-line summary (optional, Enter to skip)", default="", show_default=False
    )
    try:
        from coord import issue_store  # noqa: PLC0415

        outcome = issue_store.post_result(
            issue_store.ResultRecord(
                assignment_id=assignment_id,
                machine_name=machine_name,
                repo_name=repo_name,
                repo_github=repo_github,
                issue_number=int(issue_number),
                status="done",
                verdict=verdict,  # type: ignore[arg-type]  # narrowed to approve/request-changes above
                summary=summary,
                branch=None,
            )
        )
        click.echo(
            f"  verdict '{verdict}' recorded (posted_to_github={outcome.posted})."
        )
        if outcome.error:
            click.echo(f"  github post warning: {outcome.error}", err=True)
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort; fall back to the hint
        click.echo(
            f"  warning: failed to record verdict inline: {exc}\n{verdict_cmd_hint}",
            err=True,
        )
        return False


@main.command(help="Directly assign an issue to a machine, bypassing coord plan.")
@click.argument("machine")
@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
@click.option("--briefing", default="", help="Optional briefing text for the worker.")
@click.option(
    "--briefing-file",
    "briefing_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help=(
        "#569: read the briefing from a file instead of --briefing. Avoids "
        "shell-quoting a multi-line briefing on the command line (a multi-line "
        "--briefing typed into a PTY shell strands it at `quote>`). Overrides "
        "--briefing when both are given."
    ),
)
@click.option(
    "--model",
    default=None,
    help="Claude model tier (haiku, sonnet, opus). Defaults to models.default.",
)
@click.option("--dry-run", is_flag=True, help="Show what would be dispatched.")
@click.option(
    "--plan-only",
    is_flag=True,
    help=(
        "Dispatch a read-only planning worker. The worker reads the codebase "
        "and outputs a structured plan (FILES_READ, FILES_MODIFY, APPROACH, "
        "RISKS, ESTIMATE) without writing code or modifying files. "
        "No worktree or feature branch is created."
    ),
)
@click.option(
    "--no-plan",
    is_flag=True,
    help=(
        "Force a direct work dispatch even when dispatch.require_plan is true "
        "in coordinator.yml. Has no effect when require_plan is false."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    help="Bypass claim detection only (use when retrying after infra failures).",
)
@click.option(
    "--no-pull",
    is_flag=True,
    help=(
        "Skip the auto-pull of stale dependency repos on the agent. "
        "The briefing still carries a 'pull these before building' "
        "addendum so the worker is aware (#267)."
    ),
)
@click.option(
    "--skip-freshness",
    is_flag=True,
    help=(
        "Skip the dependency freshness check entirely — faster, no "
        "network for GH HEADs.  Matches `coord approve --skip-freshness` (#267)."
    ),
)
@click.option(
    "--interactive",
    is_flag=True,
    help=(
        "HUMAN-ATTENDED launcher (#437): start interactive `claude` "
        "locally on THIS terminal with the briefing PRE-FILLED in the "
        "input box.  You press Enter to submit and Ctrl-C / `/exit` to "
        "end the session.  Used for the subscription-billed path; the "
        "coordinator does NOT watch the TTY, does NOT auto-submit, does "
        "NOT advance the pipeline from session output.  This bypasses "
        "the agent HTTP server and runs `claude` as a child of your "
        "shell."
    ),
)
@click.option(
    "--review-of",
    "review_of",
    default=None,
    help=(
        "Launch a human-attended interactive REVIEW of completed work "
        "assignment <ID> (the work id from `coord status`). Implies a "
        "review-shaped dispatch: type=review linked to the work (so the merge "
        "gate's has_approved_review can find the verdict), the diff-only "
        "review briefing, and NO isolated worktree (read-only in the live "
        "checkout). Report your verdict with `coord report-result --verdict "
        "approve|request-changes`. Requires --interactive; local-only for now "
        "(remote review is Track B / #486)."
    ),
)
@click.option(
    "--fix-of",
    "fix_of",
    default=None,
    help=(
        "Leg 3 (#517): launch a human-attended interactive FIX for a review "
        "assignment <ID> whose verdict was request-changes. Continues on the "
        "reviewed work's EXISTING branch (so the same PR is updated, not a new "
        "orphan branch), is briefed with the reviewer's findings, and bumps "
        "review_iteration so the next review can scope to just the fix delta. "
        "ALSO accepts a WORK assignment id whose test gate FAILED (#581): the "
        "fix is then briefed with the recorded test-failure story. "
        "Requires --interactive; local-only for now (remote is Track B / #486)."
    ),
)
@click.option(
    "--troubleshoot",
    "troubleshoot",
    is_flag=True,
    default=False,
    help=(
        "#569: launch a human-attended READ-ONLY diagnostic session for a "
        "stalled item. Runs in the LIVE checkout with NO claim and NO worktree "
        "(so it never conflicts with the item's own in-progress claim), "
        "type=troubleshoot, briefed from --briefing/--briefing-file. Requires "
        "--interactive; local-only."
    ),
)
@click.option(
    "--rework-of",
    "rework_of",
    default=None,
    help=(
        "#563: launch a human-attended interactive REWORK of an existing branch. "
        "Accepts a work assignment ID (resolves its branch) or a branch name "
        "directly. Continues on the EXISTING branch (no orphan branch), seeds "
        "the session with the operator-supplied --briefing verbatim, and bumps "
        "review_iteration so the reworked branch is re-reviewed before merge. "
        "Requires --interactive and --briefing; works local and remote (same "
        "worktree + push-back as --fix-of)."
    ),
)
@click.option(
    "--smoke-of",
    "smoke_of",
    default=None,
    help=(
        "Leg 3c / A3 (#517, #350, #581): launch a human-attended interactive "
        "TESTING agent for completed work assignment <ID>. The agent lists the "
        "smoke tests, pulls the build artifact, guides you through running it, "
        "interviews you about what you saw, and records the verdict with "
        "`coord test --passed|--fail`. Read-only tools, NO worktree (runs in the "
        "live checkout). Requires --interactive; local-only for now."
    ),
)
@click.option(
    "--merge-of",
    "merge_of",
    default=None,
    help=(
        "Leg 3c (#517, #306): launch a human-attended interactive MERGE agent "
        "for completed+approved work assignment <ID>. Continues the work branch "
        "in a worktree, fetches + rebases it onto the repo's default branch "
        "(proactive rebase, #306), resolves mechanical conflicts, runs the "
        "tests, pushes --force-with-lease, then guides you to merge. Requires "
        "--interactive; local-only for now."
    ),
)
def assign(
    machine: str,
    repo: str,
    issue: int,
    config_path: Path,
    briefing: str,
    model: str | None,
    dry_run: bool,
    plan_only: bool,
    no_plan: bool,
    force: bool,
    no_pull: bool,
    skip_freshness: bool,
    interactive: bool,
    review_of: str | None,
    fix_of: str | None,
    briefing_file: str | None,
    troubleshoot: bool,
    rework_of: str | None,
    smoke_of: str | None,
    merge_of: str | None,
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

    # Refuse direct assignment to a paused machine — `coord pause` exists
    # so the user can explicitly steer work away.  If they meant to dispatch
    # anyway they should `coord unpause` first.
    from coord.machine_pause import is_paused as _is_paused
    if _is_paused(machine):
        click.echo(
            f"error: machine {machine!r} is paused; run `coord unpause {machine}` first",
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

    # --briefing-file (#569): read the briefing from a file; this avoids having
    # to shell-quote a multi-line briefing on the command line (a multi-line
    # --briefing typed into a PTY shell strands it at `quote>`).  Overrides
    # --briefing when both are given.
    if briefing_file:
        briefing = Path(briefing_file).read_text(encoding="utf-8")

    # Auto-generate briefing from issue body when none provided.
    if not briefing:
        issue_body = issue_data.get("body", "")
        if issue_body:
            briefing = f"Issue #{issue}: {issue_title}\n\n{issue_body}"

    # A1 (interactive-mode migration): --review-of is a flavour of the
    # human-attended interactive launcher, so it requires --interactive.
    if review_of is not None and not interactive:
        click.echo("error: --review-of requires --interactive", err=True)
        sys.exit(2)

    # Leg 3 (#517): --fix-of is a sibling flavour — a human-attended fix of a
    # request-changes review.  Same interactive requirement; mutually exclusive
    # with --review-of (a dispatch is one shape or the other).
    if fix_of is not None and not interactive:
        click.echo("error: --fix-of requires --interactive", err=True)
        sys.exit(2)
    if fix_of is not None and review_of is not None:
        click.echo("error: --fix-of and --review-of are mutually exclusive", err=True)
        sys.exit(2)

    # #569: --troubleshoot is a read-only diagnostic flavour — requires
    # --interactive.
    if troubleshoot and not interactive:
        click.echo("error: --troubleshoot requires --interactive", err=True)
        sys.exit(2)

    # #563: --rework-of — requires --interactive, and --briefing so the operator
    # always supplies explicit rework instructions.
    if rework_of is not None and not interactive:
        click.echo("error: --rework-of requires --interactive", err=True)
        sys.exit(2)
    if rework_of is not None and not (briefing or "").strip():
        click.echo(
            "error: --rework-of requires --briefing (supply the rework instructions).",
            err=True,
        )
        sys.exit(2)

    # Leg 3c (#517): --smoke-of (interactive testing agent) and --merge-of
    # (interactive merge agent) — each requires --interactive.
    if smoke_of is not None and not interactive:
        click.echo("error: --smoke-of requires --interactive", err=True)
        sys.exit(2)
    if merge_of is not None and not interactive:
        click.echo("error: --merge-of requires --interactive", err=True)
        sys.exit(2)

    # All interactive flavours are mutually exclusive — a dispatch is exactly
    # one shape (review / fix / troubleshoot / rework / smoke / merge).
    _interactive_flavours = [
        ("--review-of", review_of is not None),
        ("--fix-of", fix_of is not None),
        ("--troubleshoot", troubleshoot),
        ("--rework-of", rework_of is not None),
        ("--smoke-of", smoke_of is not None),
        ("--merge-of", merge_of is not None),
    ]
    _set_flavours = [name for name, on in _interactive_flavours if on]
    if len(_set_flavours) > 1:
        click.echo(
            f"error: {', '.join(_set_flavours)} are mutually exclusive "
            "(a dispatch is exactly one shape).",
            err=True,
        )
        sys.exit(2)

    # #437: HUMAN-ATTENDED branch.  When --interactive is set, we run
    # interactive `claude` as a child of THIS shell with the briefing
    # PRE-FILLED in the input box.  No HTTP agent, no Proposal, no
    # GitHub posting, no board update — the operator drives the session
    # and closes it manually.  This is the subscription-billed escape
    # hatch from Anthropic ToS §3.7 metering.  Resolving
    # ClaudePtyProvider here AND asserting its capabilities are flagged
    # human_attended_only is the structural guarantee that this path is
    # the only one that can launch it; the unattended dispatch sites
    # (dispatch/review/reconcile) refuse the same capability.
    if interactive:
        # #466: The interactive launcher path now CLAIMS the issue and
        # RECORDS the dispatched assignment up front (it used to write
        # nothing then sys.exit), and on session exit invokes the
        # git-floor backstop in :func:`finalize_interactive_exit` so the
        # board ALWAYS gets a terminal completion — even if the human
        # closed the TTY without typing `coord report-result`.  Both the
        # backstop and the report-result subcommand write through the
        # single :mod:`coord.issue_store` seam so the future #183
        # IssueStore + coordination MCP can slot in without changing any
        # of these call sites.
        import time as _time  # noqa: PLC0415
        import uuid as _uuid  # noqa: PLC0415

        from coord.providers import ClaudePtyProvider  # noqa: PLC0415
        from coord.interactive import (  # noqa: PLC0415
            TmuxHost,
            _launch_via_tmux as _tmux_launch,
            finalize_interactive_exit,
            finalize_remote_interactive_exit,
            launch_human_attended_interactive,
            tmux_available as _tmux_avail,
            tmux_session_alive as _tmux_alive,
            tmux_session_name as _tmux_name,
        )

        provider = ClaudePtyProvider()
        caps = provider.capabilities()
        # Structural guard: confirm we wired the right backend.
        # Use RuntimeError (not assert) so this is never silently removed
        # when Python runs with -O.
        if not caps.human_attended_only:
            raise RuntimeError(
                "BUG: --interactive resolved a provider whose capabilities do "
                "NOT report human_attended_only=True; refusing to launch."
            )

        # Detect whether the target machine is the local machine so we can
        # choose the local TTY path vs the remote SSH+tmux path (#494).
        # Mirrors the hostname-matching logic in _save_config_snapshot.
        _local_hn = socket.gethostname().split(".")[0].lower()
        _is_local = (
            machine_obj.name.lower() == _local_hn
            or machine_obj.host.split(".")[0].lower() == _local_hn
        )

        # #590: on a thin client the local board/DB is empty, so resolve the
        # interactive-launch target (--review-of/--fix-of/--rework-of/--smoke-of/
        # --merge-of) from the daemon's board, and skip the local post-dispatch
        # save_board (record_dispatched_assignment already routed the row to the
        # daemon; a local save would write/resurrect an empty local coord.db).
        from coord.client import (  # noqa: PLC0415
            fetch_remote_board as _fetch_remote_board,
            resolve_board_service as _resolve_svc,
        )

        _svc = _resolve_svc()

        def _interactive_board(_local_build):
            """The board used to resolve a launch target: remote when a board
            service is set, else the local build."""
            return _fetch_remote_board(_svc) if _svc is not None else _local_build()

        # ── A1 (interactive-mode migration, Track A): INTERACTIVE REVIEW ────
        # `--review-of <work_aid>` launches a human-attended REVIEW of an
        # already-completed work assignment, not a fresh work session.  It
        # differs from the work/plan path in four ways (per the migration
        # plan):
        #   1. type="review" + review_of_assignment_id set, so
        #      merge_queue.has_approved_review can find the verdict;
        #   2. the diff-only build_review_briefing (not a work briefing);
        #   3. NO isolated worktree — the review is read-only (git fetch +
        #      diff), run in the LIVE checkout;
        #   4. finalize with worktree_path=None so the git-floor backstop
        #      never pushes or removes the live checkout.
        # The verdict is reported via `coord report-result --verdict`.  This
        # path is self-contained and returns, leaving the work/plan launch
        # below byte-for-byte unchanged.
        if review_of is not None:
            from coord.review import (  # noqa: PLC0415
                REVIEWER_SYSTEM_PROMPT,
                _read_repo_claude_md,
                build_review_briefing,
            )
            from coord.models import Assignment  # noqa: PLC0415
            from coord.state import (  # noqa: PLC0415
                build_board as _build_board_rv,
                record_dispatched_assignment,
                save_board as _save_board_rv,
            )
            from coord.agent import AssignmentSpec as _AssignmentSpecRv  # noqa: PLC0415

            _rv_board = _interactive_board(_build_board_rv)
            work = _rv_board.find_by_id(review_of)
            if work is None:
                click.echo(
                    f"error: --review-of {review_of}: no such assignment on the "
                    "board (use the work id from `coord status`).",
                    err=True,
                )
                sys.exit(2)
            if not work.branch:
                click.echo(
                    f"error: work assignment {review_of} has no branch to review.",
                    err=True,
                )
                sys.exit(2)

            # Track B / #486: the review runs either on the LOCAL TTY (in the
            # live checkout) or on a REMOTE machine over ssh+tmux.  A review is
            # read-only either way (no worktree, no branch mutation), so the
            # remote path is the lowest-risk Track-B leg.
            if _is_local:
                # Expand `~` — the path is handed straight to a local child cwd.
                review_repo_path = str(
                    Path(machine_obj.repo_path(repo) or str(Path.cwd())).expanduser()
                )
            else:
                # Keep the raw path so the remote shell expands `~`/$HOME itself.
                review_repo_path = machine_obj.repo_path(repo) or f"~/src/{repo}"
            review_default_branch = repo_cfg.default_branch or "main"
            resolved_model = model if model else cfg.models.default
            assignment_id = _uuid.uuid4().hex[:12]

            # CLAUDE.md: local → read the live checkout; remote → leave empty and
            # have the reviewer read ./CLAUDE.md in the remote checkout it sits
            # in (its actual rules, not the coordinator's possibly-divergent copy).
            claude_md = (
                _read_repo_claude_md(Path(review_repo_path)) if _is_local else ""
            )
            review_briefing = build_review_briefing(
                pr_number=None,
                pr_url=None,
                repo_github=repo_cfg.github,
                repo_name=repo,
                issue_number=issue,
                issue_title=issue_title,
                issue_body=issue_data.get("body", ""),
                branch=work.branch,
                worker_machine=work.machine_name or machine,
                same_as_worker=False,
                reviews_cfg=cfg.reviews,
                repo_claude_md=claude_md,
                default_branch=review_default_branch,
                review_iteration=getattr(work, "review_iteration", 0) or 0,
            )
            # Interactive reviewer reports via report-result, not the
            # REVIEW_VERDICT block the briefing's tail describes.  Since #590 the
            # remote path matches local: `coord report-result` routes to the
            # daemon's shared DB (board_service), so a remote reviewer
            # self-reports exactly like a local one and the verdict reaches the
            # merge gate from any machine.  (The host-side finalize
            # operator-prompt stays as a backstop if nothing is recorded.)
            _remote_note = (
                ""
                if _is_local
                else (
                    "running on a REMOTE machine. You are in the live checkout — "
                    "read ./CLAUDE.md (and any sub-repo CLAUDE.md) for the project "
                    "rules before reviewing. "
                )
            )
            report_reminder = (
                f"[Coordinator review assignment {assignment_id}] This is a "
                f"HUMAN-ATTENDED interactive review {_remote_note}When you finish:\n"
                "  1. Write your FULL findings (every blocking item, with "
                f"file:line) to a file, e.g. /tmp/review-{assignment_id}.md\n"
                "  2. Run:\n"
                f"     coord report-result --assignment {assignment_id} "
                "--status done --verdict approve|request-changes "
                f"--summary <one-line summary> --body-file /tmp/review-{assignment_id}.md\n"
                "The --body-file is IMPORTANT: it is what the fix worker is "
                "briefed with (the one-line --summary is not enough). Without "
                "it the fix worker has to re-derive your findings from the "
                "diff. Your `coord report-result` routes to the coordinator's "
                "shared board (#590), so the verdict reaches the merge gate from "
                "here. Do NOT run any `gh` commands; the coordinator posts the "
                "verdict + findings for you.\n\n"
            )
            effective_briefing = report_reminder + review_briefing

            spec = _AssignmentSpecRv(
                repo_name=repo,
                repo_path=review_repo_path,
                issue_number=issue,
                issue_title=f"[review] {issue_title}",
                briefing=effective_briefing,
                model=resolved_model,
                type="review",
                provider="claude-pty",
            )
            # A review is READ-ONLY: use the reviewer system prompt (not the
            # worker default build_command would otherwise apply for an
            # unrecognised type) and drop Edit/Write from the tool set so the
            # session can't mutate the live checkout.  Bash stays for
            # git fetch/diff/log; Read/Grep/Glob for inspecting the code.
            argv = provider.build_command(
                spec,
                resolved_model=resolved_model,
                system_prompt=REVIEWER_SYSTEM_PROMPT,
                allowed_tools="Read,Bash,Grep,Glob",
            )
            # Remote: a bare "claude" is not on the SSH login PATH (#424/#425);
            # swap argv[0] for the absolute path the remote shell can find.
            if not _is_local:
                argv = ["~/.local/bin/claude"] + list(argv)[1:]

            _rv_location = (
                "local TTY" if _is_local
                else f"{machine_obj.host} (remote tmux)"
            )
            click.echo(
                f"{machine} ({_rv_location}) → REVIEW of #{issue} "
                f"on branch {work.branch}: {issue_title}"
            )
            click.echo(
                "  mode: HUMAN-ATTENDED interactive review "
                "(migration A1 / Track B #486)"
            )
            click.echo(
                f"  assignment id: {assignment_id}  (review_of={review_of})"
            )
            if _is_local:
                click.echo(
                    f"  cwd: {review_repo_path} (live checkout — read-only, "
                    "no worktree)"
                )
            else:
                click.echo(
                    f"  remote checkout: {review_repo_path} on "
                    f"{machine_obj.host} (read-only, no worktree)"
                )
            if dry_run:
                click.echo("  (dry run — not launched)")
                click.echo(f"  would exec: {argv}")
                return

            review_assignment = Assignment(
                machine_name=machine,
                repo_name=repo,
                issue_number=issue,
                issue_title=f"[review] {issue_title}",
                briefing=effective_briefing,
                assignment_id=assignment_id,
                status="running",
                branch=work.branch,
                dispatched_at=_time.time(),
                type="review",
                review_of_assignment_id=review_of,
                review_target=work.branch,
                model=resolved_model,
                provider_name="claude-pty",
            )
            record_dispatched_assignment(
                assignment=review_assignment, repo_github=repo_cfg.github
            )
            if _svc is None:
                _save_board_rv(_build_board_rv())
            os.environ["COORD_ASSIGNMENT_ID"] = assignment_id

            started_at = _time.time()
            if _is_local:
                exit_code = launch_human_attended_interactive(
                    argv,
                    effective_briefing,
                    assignment_id=assignment_id,
                    cwd=review_repo_path,
                )
                if exit_code != 0:
                    click.echo(
                        f"  claude exited with status {exit_code}", err=True
                    )

                _sname = _tmux_name(assignment_id) if _tmux_avail() else None
                if _sname and _tmux_alive(_sname):
                    click.echo(
                        f"  session still running in tmux: {_sname}\n"
                        f"  reattach with:  coord reattach {assignment_id}"
                    )
                    sys.exit(0)

                # finalize with worktree_path=None — the backstop must never push
                # or remove the live checkout.  If the reviewer ran
                # `coord report-result --verdict`, finalize sees the terminal row
                # and leaves the verdict untouched.
                try:
                    finalize_result = finalize_interactive_exit(
                        assignment_id=assignment_id,
                        repo_name=repo,
                        repo_github=repo_cfg.github,
                        issue_number=issue,
                        machine_name=machine,
                        worktree_path=None,
                        base_branch=review_default_branch,
                        exit_code=exit_code,
                        started_at=started_at,
                        log_path=None,
                        repo_path=None,
                    )
                    if finalize_result.already_recorded:
                        click.echo("  verdict recorded via `coord report-result`")
                    else:
                        # The reviewer exited without running `coord
                        # report-result`.  Mirror the remote path (#486d): prompt
                        # the operator here (this is a TTY) and relay the verdict
                        # through the same issue_store seam, so the merge gate /
                        # Fix routing sees it instead of silently stalling on a
                        # missing verdict.
                        _verdict_cmd = (
                            f"    coord report-result --assignment {assignment_id} "
                            "--status done --verdict approve|request-changes "
                            "--summary <one-line summary>"
                        )
                        if not _prompt_and_relay_review_verdict(
                            assignment_id=assignment_id,
                            repo_name=repo,
                            repo_github=repo_cfg.github,
                            issue_number=issue,
                            machine_name=machine,
                            verdict_cmd_hint=_verdict_cmd,
                        ):
                            click.echo(
                                "  review session ended with no verdict reported "
                                f"(status={finalize_result.terminal_status}) — the "
                                "merge gate stays blocked until a verdict is "
                                "reported."
                            )
                except Exception as exc:  # noqa: BLE001 — best-effort backstop
                    click.echo(
                        f"  warning: backstop failed to record review exit: {exc}",
                        err=True,
                    )
                return

            # ── REMOTE REVIEW (Track B / #486) ────────────────────────────
            # Read-only: cd into the remote LIVE checkout, fetch+prune so the
            # reviewer can diff origin/<branch>, then launch the reviewer.  NO
            # worktree and NO branch mutation — the live checkout is the worker
            # worktree base and must not be disturbed.  Verdict comes back via
            # the operator running `coord report-result` on THIS coordinator
            # (the assignment row lives here); a remote report-result would
            # write the remote DB and never reach the merge gate (#486d).
            import shlex as _shlex_rv  # noqa: PLC0415

            _rp_sh = (
                "$HOME/" + review_repo_path[2:]
                if review_repo_path.startswith("~/")
                else ("$HOME" if review_repo_path == "~" else review_repo_path)
            )
            _claude_args = _shlex_rv.join(list(argv)[1:])
            _remote_cmd = (
                f"cd {_rp_sh}"
                f" && git fetch origin --prune 2>/dev/null || true"
                f" && COORD_ASSIGNMENT_ID={assignment_id} {argv[0]} {_claude_args}"
            )
            _tmux_host = TmuxHost(ssh_target=machine_obj.host)
            _sname = _tmux_name(assignment_id)

            # Echo the briefing to the LOCAL terminal before attaching, so the
            # operator can read it before pressing Enter (mirrors the remote
            # work path).
            if effective_briefing.strip():
                _hdr = (
                    "--- seeded briefing -- review below; "
                    "submit the pre-filled input in Claude to send ---"
                )
                _ftr = "-" * len(_hdr)
                _preview = f"\n{_hdr}\n{effective_briefing.rstrip()}\n{_ftr}\n\n"
                try:
                    os.write(sys.stdout.fileno(), _preview.encode("utf-8"))
                except OSError:
                    pass

            _rc = _tmux_launch(
                argv,
                effective_briefing,
                _sname,
                cwd=None,
                host=_tmux_host,
                raw_shell_cmd=_remote_cmd,
            )
            if _rc is None:
                click.echo(
                    "  error: could not create remote tmux session on "
                    f"{machine_obj.host}",
                    err=True,
                )
                sys.exit(1)
            exit_code = _rc
            if exit_code != 0:
                click.echo(f"  claude exited with status {exit_code}", err=True)

            _still_alive = _tmux_alive(_sname, host=_tmux_host)
            # Verdict-out (#486d): the verdict is recorded on THIS coordinator,
            # where the assignment row lives and the merge gate reads
            # `review_verdict` — never on the remote machine's DB.
            _verdict_cmd = (
                f"    coord report-result --assignment {assignment_id} "
                "--status done --verdict approve|request-changes "
                "--summary <one-line summary>"
            )
            if _still_alive:
                click.echo(
                    f"  session still running in remote tmux: {_sname}\n"
                    f"  reattach with:  ssh -t {machine_obj.host}"
                    f" tmux attach-session -t {_sname}"
                )
                click.echo(
                    "  to record the verdict (the merge gate keys on it), run ON "
                    f"THIS coordinator:\n{_verdict_cmd}"
                )
                sys.exit(0)

            # Session ended.  Record a terminal state so the review row does NOT
            # linger as a phantom 'running' worker that holds the issue claim
            # forever — the bug this path used to have (it printed the verdict
            # reminder and exited, never going terminal).  A review is
            # read-only, so finalize with worktree_path=None / repo_path=None:
            # the backstop only writes the coordinator DB (no push, no worktree
            # touch), identical to the local review path above.  An operator
            # `coord report-result` is respected (already_recorded → no clobber).
            try:
                finalize_result = finalize_interactive_exit(
                    assignment_id=assignment_id,
                    repo_name=repo,
                    repo_github=repo_cfg.github,
                    issue_number=issue,
                    machine_name=machine,
                    worktree_path=None,
                    base_branch=review_default_branch,
                    exit_code=exit_code,
                    started_at=started_at,
                    log_path=None,
                    repo_path=None,
                )
                if finalize_result.already_recorded:
                    click.echo("  verdict recorded via `coord report-result`")
                else:
                    # #486d: don't leave the verdict as a manual step — prompt the
                    # operator here (on the coordinator, where the row lives) and
                    # relay it, so the merge gate / leg-3 Fix routing sees it.
                    _prompt_and_relay_review_verdict(
                        assignment_id=assignment_id,
                        repo_name=repo,
                        repo_github=repo_cfg.github,
                        issue_number=issue,
                        machine_name=machine,
                        verdict_cmd_hint=_verdict_cmd,
                    )
            except Exception as exc:  # noqa: BLE001 — best-effort backstop
                click.echo(
                    f"  warning: backstop failed to record review exit: {exc}",
                    err=True,
                )
            sys.exit(exit_code)

        # ── Leg 3c / A3 (#517, #350, #581): --smoke-of <work_aid> ─────────
        # A human-attended interactive TESTING agent for an already-completed
        # work assignment.  Like --review-of it is READ-ONLY and runs in the
        # LIVE checkout (no isolated worktree): the agent pulls the build
        # artifact, lists the smoke tests, walks the operator through running
        # them, interviews about what was seen, and records the verdict via
        # `coord test --passed|--fail <work_aid>` (which writes test_state /
        # smoke_test on the WORK row — exactly what the merge gate and the TUI
        # Test stage read).  The session row itself reports done via
        # report-result.  Self-contained and returns.  Local-only (Track B / #486).
        if smoke_of is not None:
            from coord.models import Assignment as _AssignmentSm  # noqa: PLC0415
            from coord.state import (  # noqa: PLC0415
                build_board as _build_board_sm,
                get_test_plan as _get_test_plan_sm,
                record_dispatched_assignment as _record_sm,
                save_board as _save_board_sm,
            )
            from coord.agent import AssignmentSpec as _AssignmentSpecSm  # noqa: PLC0415

            _sm_board = _interactive_board(_build_board_sm)
            work = _sm_board.find_by_id(smoke_of)
            if work is None:
                click.echo(
                    f"error: --smoke-of {smoke_of}: no such assignment on the "
                    "board (use the work id from `coord status`).",
                    err=True,
                )
                sys.exit(2)
            if not work.branch:
                click.echo(
                    f"error: work assignment {smoke_of} has no branch to test.",
                    err=True,
                )
                sys.exit(2)

            if not _is_local:
                click.echo(
                    "error: --smoke-of is local-only for now; run it on the "
                    "machine that holds the checkout (remote interactive smoke "
                    "is Track B / #486).",
                    err=True,
                )
                sys.exit(2)

            smoke_repo_path = str(
                Path(machine_obj.repo_path(repo) or str(Path.cwd())).expanduser()
            )
            smoke_default_branch = repo_cfg.default_branch or "main"
            resolved_model = model if model else cfg.models.default
            assignment_id = _uuid.uuid4().hex[:12]

            # Surface the cached smoke-test plan (#342) when one exists so the
            # agent can lead with the concrete steps instead of re-deriving them.
            try:
                _plan = _get_test_plan_sm(smoke_of)
            except Exception:  # noqa: BLE001
                _plan = None
            if _plan and isinstance(_plan, dict) and _plan.get("steps"):
                import json as _json_sm  # noqa: PLC0415
                _plan_block = (
                    "A cached smoke-test plan exists for this branch:\n\n"
                    "```json\n" + _json_sm.dumps(_plan, indent=2) + "\n```\n"
                )
            else:
                _plan_block = (
                    "No cached smoke-test plan was found. Run "
                    f"`coord test-plan {smoke_of}` to generate one (it reads the "
                    "PR diff, the repo's CLAUDE.md and the artifact manifest), "
                    "then read it back to the operator.\n"
                )

            INTERACTIVE_SMOKE_SYSTEM_PROMPT = (
                "You are a human-attended smoke-test guide dispatched by the "
                "coordinator. A human operator is at the keyboard with you. Your "
                "job is to walk them through validating a completed branch and "
                "then record their verdict.\n\n"
                "Rules:\n"
                "- Do NOT modify code, push commits, or open/merge PRs. You only "
                "help validate. (Edit/Write are not available to you.)\n"
                "- Do NOT run `gh` commands. The coordinator owns GitHub.\n"
                "- You MAY run git (read-only), build/run commands, and the "
                "`coord pull-artifact` / `coord test-plan` / `coord test` "
                "commands.\n"
                "- Keep it conversational: propose ONE concrete next command at a "
                "time, wait for the operator to run it (or run it yourself when "
                "it's safe and read-only) and tell you what they saw.\n\n"
                "Flow:\n"
                "1. Read the smoke-test plan (below, or generate one). List the "
                "checks for the operator.\n"
                "2. Offer to pull the prebuilt artifact for this branch with "
                "`coord pull-artifact <work_aid>` so they don't have to rebuild.\n"
                "3. Walk through each check. Ask what they observed. If something "
                "is wrong, interview them for a clear repro (expected vs actual, "
                "suspected area/files) — this becomes the fix brief.\n"
                "4. When every check has a clear position, record the verdict:\n"
                "   - All good  → run `coord test --passed <work_aid>`\n"
                "   - Broken    → run `coord test --fail <work_aid> --reason "
                "\"<story>\"` where <story> is the COMPLETE failure brief the fix "
                "worker needs: what was checked, expected vs actual, the repro "
                "steps, and the suspected files/area — not just one line. This "
                "reason IS what the fix worker is briefed with, so make it "
                "self-contained.\n"
                "   Then tell the operator exactly what happens next (the TUI "
                "will offer the fix or merge step).\n"
            )

            smoke_briefing = (
                f"# Smoke-test assignment: {repo_cfg.github} #{issue}\n\n"
                f"**Issue:** {issue_title}\n"
                f"**Branch under test:** `{work.branch}` "
                f"(worker: {work.machine_name or machine})\n"
                f"**Work assignment id (use this for `coord test` / "
                f"`coord pull-artifact`):** `{smoke_of}`\n"
                f"**Repo checkout:** {smoke_repo_path}\n"
                f"**Default branch:** {smoke_default_branch}\n\n"
                "## ⚠ Do NOT move this checkout's branch (#601)\n\n"
                f"`{smoke_repo_path}` is the **live checkout that runs the "
                "coordinator itself** (and the worktree base for workers). Do "
                "**NOT** `git checkout` / `git switch` / `git reset` / "
                "`git stash` it — checking out the branch here silently "
                "downgrades the running `coord` to this branch's code until it's "
                "restored. To inspect the branch under test WITHOUT moving it:\n"
                f"  - `git -C {smoke_repo_path} fetch origin && "
                f"git -C {smoke_repo_path} diff {smoke_default_branch}...origin/{work.branch}`\n"
                f"  - `git -C {smoke_repo_path} show origin/{work.branch}:<path>` for a single file\n"
                f"  - or make your OWN scratch worktree: "
                f"`git -C {smoke_repo_path} worktree add /tmp/smoke-{smoke_of} origin/{work.branch}` "
                f"(remove it with `git -C {smoke_repo_path} worktree remove /tmp/smoke-{smoke_of}` when done)\n"
                "  - prefer `coord pull-artifact` (above) for the prebuilt binary.\n\n"
                f"## Issue body\n\n{issue_data.get('body', '') or '(none)'}\n\n"
                f"## Smoke-test plan\n\n{_plan_block}\n"
                "## Your job\n\n"
                "Guide the operator through validating this branch (see the "
                "system prompt for the flow), then record the verdict with "
                f"`coord test --passed {smoke_of}` or `coord test --fail "
                f"{smoke_of} --reason \"...\"`.\n"
            )

            report_reminder = (
                f"[Coordinator smoke assignment {assignment_id}] HUMAN-ATTENDED "
                "interactive smoke test. Record the operator's verdict with "
                f"`coord test --passed {smoke_of}` or `coord test --fail "
                f"{smoke_of} --reason \"...\"`. When you exit, also run "
                f"`coord report-result --assignment {assignment_id} --status done "
                "--summary <one-line summary>` so this session's row closes.\n\n"
            )
            effective_briefing = report_reminder + smoke_briefing

            spec = _AssignmentSpecSm(
                repo_name=repo,
                repo_path=smoke_repo_path,
                issue_number=issue,
                issue_title=f"[smoke] {issue_title}",
                briefing=effective_briefing,
                model=resolved_model,
                type="smoke",
                provider="claude-pty",
            )
            # READ-ONLY like --review-of: no Edit/Write — the smoke agent
            # validates, it does not fix.  Bash stays for build/run + the
            # coord helper commands; Read/Grep/Glob for inspecting the code.
            argv = provider.build_command(
                spec,
                resolved_model=resolved_model,
                system_prompt=INTERACTIVE_SMOKE_SYSTEM_PROMPT,
                allowed_tools="Read,Bash,Grep,Glob",
            )

            click.echo(
                f"{machine} (local TTY) → SMOKE TEST of #{issue} "
                f"on branch {work.branch}: {issue_title}"
            )
            click.echo("  mode: HUMAN-ATTENDED interactive smoke test (leg 3c / A3)")
            click.echo(
                f"  assignment id: {assignment_id}  (smoke_of={smoke_of})"
            )
            click.echo(
                f"  cwd: {smoke_repo_path} (live checkout — read-only, no worktree)"
            )
            if dry_run:
                click.echo("  (dry run — not launched)")
                click.echo(f"  would exec: {argv}")
                return

            smoke_assignment = _AssignmentSm(
                machine_name=machine,
                repo_name=repo,
                issue_number=issue,
                issue_title=f"[smoke] {issue_title}",
                briefing=effective_briefing,
                assignment_id=assignment_id,
                status="running",
                branch=work.branch,
                dispatched_at=_time.time(),
                type="smoke",
                review_of_assignment_id=smoke_of,
                review_target=work.branch,
                model=resolved_model,
                provider_name="claude-pty",
            )
            _record_sm(assignment=smoke_assignment, repo_github=repo_cfg.github)
            if _svc is None:
                _save_board_sm(_build_board_sm())
            os.environ["COORD_ASSIGNMENT_ID"] = assignment_id

            started_at = _time.time()
            exit_code = launch_human_attended_interactive(
                argv,
                effective_briefing,
                assignment_id=assignment_id,
                cwd=smoke_repo_path,
            )
            if exit_code != 0:
                click.echo(f"  claude exited with status {exit_code}", err=True)

            _sname = _tmux_name(assignment_id) if _tmux_avail() else None
            if _sname and _tmux_alive(_sname):
                click.echo(
                    f"  session still running in tmux: {_sname}\n"
                    f"  reattach with:  coord reattach {assignment_id}"
                )
                sys.exit(0)

            # worktree_path=None: read-only smoke runs in the live checkout, the
            # backstop must never push or remove it.  The verdict that matters
            # is the `coord test` write on the WORK row, not this session's row.
            try:
                finalize_interactive_exit(
                    assignment_id=assignment_id,
                    repo_name=repo,
                    repo_github=repo_cfg.github,
                    issue_number=issue,
                    machine_name=machine,
                    worktree_path=None,
                    base_branch=smoke_default_branch,
                    exit_code=exit_code,
                    started_at=started_at,
                    log_path=None,
                    repo_path=None,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort backstop
                click.echo(
                    f"  warning: backstop failed to record smoke exit: {exc}",
                    err=True,
                )
            return

        # ── Leg 3 (#517): --fix-of <review_aid> ──────────────────────────
        # A human-attended FIX of a request-changes review.  Continues on the
        # reviewed work's EXISTING branch (updates the same PR, never an orphan
        # branch — the fix-retry-new-branch trap), is briefed with the
        # reviewer's findings, and bumps review_iteration so the next review can
        # scope to just the fix delta.  Self-contained and returns, leaving the
        # work/plan launch below unchanged.  Local-only for now (Track B / #486).
        # ── #569: TROUBLESHOOT — read-only diagnostic interactive session ───
        # A human-attended diagnostic for a stalled item.  Like --review-of it
        # runs READ-ONLY in the LIVE checkout (no claim, no worktree, finalize
        # worktree_path=None) so it never disturbs the item's in-progress claim
        # or the worker-worktree base — but it carries the caller-supplied
        # diagnostic briefing (--briefing/--briefing-file), uses
        # type="troubleshoot", and has no verdict.  Local-only.
        if troubleshoot:
            from coord.models import Assignment as _AssignmentTs  # noqa: PLC0415
            from coord.state import (  # noqa: PLC0415
                build_board as _build_board_ts,
                record_dispatched_assignment as _record_ts,
                save_board as _save_board_ts,
            )
            from coord.agent import AssignmentSpec as _AssignmentSpecTs  # noqa: PLC0415

            if not (briefing or "").strip():
                click.echo(
                    "error: --troubleshoot requires a briefing "
                    "(--briefing or --briefing-file).",
                    err=True,
                )
                sys.exit(2)
            if not _is_local:
                click.echo("error: --troubleshoot is local-only for now.", err=True)
                sys.exit(2)

            ts_repo_path = str(
                Path(machine_obj.repo_path(repo) or str(Path.cwd())).expanduser()
            )
            ts_default_branch = repo_cfg.default_branch or "main"
            resolved_model = model if model else cfg.models.default
            assignment_id = _uuid.uuid4().hex[:12]

            _ts_system_prompt = (
                "You are a coordinator troubleshooter in a HUMAN-ATTENDED "
                "session. You are READ-ONLY in a live checkout: do NOT modify "
                "files, do NOT commit, do NOT run `gh`. Investigate the stalled "
                "pipeline item using coord, git, and sqlite3 reads; explain "
                "what is wrong and what will unstick it; and surface any plan "
                "for the operator to approve before mutating anything."
            )
            ts_reminder = (
                f"[Coordinator troubleshoot assignment {assignment_id}] "
                "HUMAN-ATTENDED, READ-ONLY diagnostic for a stalled pipeline "
                "item. You are in the LIVE checkout — do NOT modify files here "
                "(it is the editable coordinator and the worker-worktree base). "
                "If a code fix is needed, surface the plan so the operator can "
                "dispatch a proper Fix.\n\n"
            )
            effective_briefing = ts_reminder + briefing

            # Pre-fill a SHORT, single-line prompt that points the session at
            # the full diagnostic on disk, rather than pasting the whole
            # multi-line briefing into the input box.  A short paste lands
            # reliably; a multi-KB multi-line paste over the embedded-terminal /
            # nested-tmux path often is dropped by the readiness poll
            # (interactive._inject_briefing_into_tmux_session is best-effort).
            # And it degrades gracefully — if the paste is missed, the operator
            # can type the one short line by hand instead of being stranded with
            # no context.  The full briefing still lives in the file and on the
            # assignment row.
            if briefing_file:
                _ts_brief_path = str(Path(briefing_file).expanduser())
            else:
                import tempfile as _tempfile  # noqa: PLC0415

                _ts_brief_path = str(
                    Path(_tempfile.gettempdir()) / f"coord-troubleshoot-{issue}.md"
                )
                Path(_ts_brief_path).write_text(effective_briefing, encoding="utf-8")
            seed_prompt = (
                f"Troubleshoot {repo} #{issue}: read the diagnostic briefing at "
                f"{_ts_brief_path} (board state, assignments, merge-queue, CI, and a "
                "playbook of likely causes), then tell me what's wrong and the "
                "options to unstick it. You are read-only — do not modify files, "
                "commit, or run gh; surface any fix plan for me to approve."
            )

            spec = _AssignmentSpecTs(
                repo_name=repo,
                repo_path=ts_repo_path,
                issue_number=issue,
                issue_title=f"[troubleshoot] {issue_title}",
                briefing=effective_briefing,
                model=resolved_model,
                type="troubleshoot",
                provider="claude-pty",
            )
            # READ-ONLY: no Edit/Write (the live checkout must not be mutated).
            # Bash for coord/git/sqlite3 reads; Read/Grep/Glob for inspection.
            argv = provider.build_command(
                spec,
                resolved_model=resolved_model,
                system_prompt=_ts_system_prompt,
                allowed_tools="Read,Bash,Grep,Glob",
            )

            click.echo(
                f"{machine} (local TTY) → TROUBLESHOOT #{issue}: {issue_title}"
            )
            click.echo(
                "  mode: HUMAN-ATTENDED interactive diagnostic "
                "(read-only, no claim, no worktree) (#569)"
            )
            click.echo(f"  assignment id: {assignment_id}")
            click.echo(f"  cwd: {ts_repo_path} (live checkout — read-only)")
            if dry_run:
                click.echo("  (dry run — not launched)")
                click.echo(f"  would exec: {argv}")
                return

            ts_assignment = _AssignmentTs(
                machine_name=machine,
                repo_name=repo,
                issue_number=issue,
                issue_title=f"[troubleshoot] {issue_title}",
                briefing=effective_briefing,
                assignment_id=assignment_id,
                status="running",
                dispatched_at=_time.time(),
                type="troubleshoot",
                model=resolved_model,
                provider_name="claude-pty",
            )
            _record_ts(assignment=ts_assignment, repo_github=repo_cfg.github)
            _save_board_ts(_build_board_ts())
            os.environ["COORD_ASSIGNMENT_ID"] = assignment_id

            started_at = _time.time()
            # Pre-fill the SHORT seed prompt (not the full briefing) — see the
            # seed_prompt rationale above.
            exit_code = launch_human_attended_interactive(
                argv,
                seed_prompt,
                assignment_id=assignment_id,
                cwd=ts_repo_path,
            )
            if exit_code != 0:
                click.echo(f"  claude exited with status {exit_code}", err=True)

            _sname = _tmux_name(assignment_id) if _tmux_avail() else None
            if _sname and _tmux_alive(_sname):
                click.echo(
                    f"  session still running in tmux: {_sname}\n"
                    f"  reattach with:  coord reattach {assignment_id}"
                )
                sys.exit(0)

            # finalize with worktree_path=None — read-only, never push/remove the
            # live checkout; the git-floor backstop just records a terminal row.
            try:
                finalize_interactive_exit(
                    assignment_id=assignment_id,
                    repo_name=repo,
                    repo_github=repo_cfg.github,
                    issue_number=issue,
                    machine_name=machine,
                    worktree_path=None,
                    base_branch=ts_default_branch,
                    exit_code=exit_code,
                    started_at=started_at,
                    log_path=None,
                    repo_path=None,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort backstop
                click.echo(
                    f"  warning: backstop failed to record troubleshoot exit: {exc}",
                    err=True,
                )
            return

        if fix_of is not None:
            from coord.auto_loop import (  # noqa: PLC0415
                _build_fix_briefing,
                _load_review_findings,
            )
            from coord.agent import (  # noqa: PLC0415
                AssignmentSpec as _AssignmentSpecFx,
                _GitError as _AgentGitErrorFx,
                setup_interactive_worktree as _setup_wt_fx,
            )
            from coord.models import Assignment as _AssignmentFx  # noqa: PLC0415
            from coord.state import (  # noqa: PLC0415
                COORD_DIR as _COORD_DIR_FX,
                build_board as _build_board_fx,
                record_dispatched_assignment as _record_fx,
                save_board as _save_board_fx,
            )

            # Track B / #486: a fix runs on the LOCAL TTY or on a REMOTE
            # machine over ssh+tmux.  Unlike review, a fix WRITES — it needs a
            # worktree on the EXISTING branch and its commits must be pushed
            # back to origin (the #486d push-back, via
            # finalize_remote_interactive_exit; the local finalize only sees
            # the local filesystem).
            _fx_board = _interactive_board(_build_board_fx)
            review = _fx_board.find_by_id(fix_of)
            if review is None:
                click.echo(
                    f"error: --fix-of {fix_of}: no such assignment on the board.",
                    err=True,
                )
                sys.exit(2)
            # Two accepted shapes for --fix-of (#581):
            #   (a) a REVIEW assignment whose verdict was request-changes — the
            #       original leg-3a path; work = review.review_of_assignment_id,
            #       findings = the reviewer's findings.
            #   (b) a WORK assignment whose Test gate FAILED — the test-fail fix
            #       front door; work = the target itself, findings = the recorded
            #       test-failure story (test_reason).
            _fix_from_test_fail = (
                review.type != "review"
                and (getattr(review, "test_state", None) == "failed")
            )
            if review.type != "review" and not _fix_from_test_fail:
                click.echo(
                    f"error: --fix-of {fix_of} is type={review.type!r} with "
                    f"test_state={getattr(review, 'test_state', None)!r}. Pass "
                    "either a REVIEW id whose verdict was request-changes, or a "
                    "WORK id whose Test gate failed.",
                    err=True,
                )
                sys.exit(2)
            if _fix_from_test_fail:
                work = review  # the failed work row IS the thing to fix
            else:
                work = (
                    _fx_board.find_by_id(review.review_of_assignment_id)
                    if review.review_of_assignment_id
                    else None
                )
            if work is None:
                click.echo(
                    f"error: review {fix_of} has no linked work assignment "
                    "(review_of_assignment_id is unset).",
                    err=True,
                )
                sys.exit(2)
            if not work.branch:
                click.echo(
                    f"error: work assignment {work.assignment_id} has no branch "
                    "to fix.",
                    err=True,
                )
                sys.exit(2)

            # Iteration accounting mirrors the auto-loop fix path so the merge
            # gate and the next review see an identical work→fix→review chain.
            next_iteration = (work.review_iteration or 0) + 1
            max_iter = cfg.pipeline.max_review_iterations
            if next_iteration > max_iter:
                click.echo(
                    f"error: max_review_iterations ({max_iter}) reached for "
                    f"work {work.assignment_id}; not dispatching another fix. "
                    "Resolve manually or bump pipeline.max_review_iterations.",
                    err=True,
                )
                sys.exit(2)

            # Findings: reuse the SAME loader the claude -p fix path uses (DB
            # cache → log → agent).  Local-only ⇒ no machine_host.  Fall back to
            # a pointer-to-the-review brief when nothing structured was captured
            # (interactive reviews may report only a one-line verdict summary).
            if _fix_from_test_fail:
                # The findings ARE the recorded test-failure story (#581).  No
                # reviewer log to consult — the operator's `coord test --fail
                # --reason` text is what the fix worker needs.
                _test_story = (getattr(work, "test_reason", None) or "").strip()
                _findings_body = (
                    "The manual smoke test FAILED. The operator reported:\n\n"
                    f"> {_test_story}\n\n"
                    "Reproduce the failure, fix the root cause, and re-validate "
                    "before pushing."
                    if _test_story
                    else (
                        "The manual smoke test FAILED (no reason text was "
                        "recorded). Pull the branch, reproduce the failure the "
                        "operator hit, and fix the root cause before pushing."
                    )
                )
            else:
                _fx_log = _COORD_DIR_FX / "logs" / f"{fix_of}.log"
                _fx_log_path = str(_fx_log) if _fx_log.exists() else None
                try:
                    findings = _load_review_findings(
                        review, _fx_log_path, None, repo_github=repo_cfg.github,
                    )
                except Exception:  # noqa: BLE001 — best-effort; fall back below
                    findings = None
                if findings is not None and (getattr(findings, "body", "") or "").strip():
                    _findings_body = findings.body.strip()
                else:
                    _findings_body = (
                        f"(No structured findings were captured for review {fix_of}.) "
                        f"The review verdict was {review.review_verdict or 'request-changes'!r}. "
                        "Read the reviewer's feedback on the PR / issue and address "
                        "every blocking item before pushing."
                    )
            from types import SimpleNamespace as _SNS  # noqa: PLC0415
            fix_briefing = _build_fix_briefing(
                work, _SNS(body=_findings_body), next_iteration, max_iter,
            )

            resolved_model = model if model else cfg.models.default
            assignment_id = _uuid.uuid4().hex[:12]
            if _is_local:
                fix_repo_path = str(
                    Path(machine_obj.repo_path(repo) or str(Path.cwd())).expanduser()
                )
            else:
                fix_repo_path = machine_obj.repo_path(repo) or f"~/src/{repo}"
            fix_default_branch = repo_cfg.default_branch or "main"

            os.environ["COORD_ASSIGNMENT_ID"] = assignment_id
            if _is_local:
                report_reminder = (
                    f"[Coordinator fix assignment {assignment_id}] HUMAN-ATTENDED "
                    f"fix iteration {next_iteration}/{max_iter} on branch "
                    f"{work.branch}. Before you exit, run `coord report-result "
                    f"--assignment {assignment_id} --status done --summary <text>` "
                    "so the coordinator records the result and can re-review.\n\n"
                )
            else:
                report_reminder = (
                    f"[Coordinator fix assignment {assignment_id}] HUMAN-ATTENDED "
                    f"fix iteration {next_iteration}/{max_iter} on branch "
                    f"{work.branch}, running on a REMOTE machine. Make your "
                    "changes and COMMIT them. Before you exit, run `coord "
                    f"report-result --assignment {assignment_id} --status done "
                    "--summary <text>` — since #590 it routes to the "
                    "coordinator's shared board, so the result is recorded from "
                    "here. Do NOT run any `gh` commands. When you exit, the "
                    f"coordinator also pushes your commits to origin/{work.branch} "
                    "and re-reviews.\n\n"
                )
            effective_briefing = report_reminder + fix_briefing

            spec = _AssignmentSpecFx(
                repo_name=repo,
                repo_path=fix_repo_path,
                issue_number=issue,
                issue_title=f"[fix-{next_iteration}] {issue_title}",
                briefing=effective_briefing,
                model=resolved_model,
                type="work",
                provider="claude-pty",
            )
            # type="work" ⇒ default worker tool set (Read/Edit/Write/Bash): the
            # fix session must be able to mutate the checkout, unlike --review-of.
            argv = provider.build_command(spec, resolved_model=resolved_model)
            # Remote: bare "claude" isn't on the SSH login PATH (#424/#425).
            if not _is_local:
                argv = ["~/.local/bin/claude"] + list(argv)[1:]

            _fx_location = (
                "local TTY" if _is_local
                else f"{machine_obj.host} (remote tmux)"
            )
            click.echo(
                f"{machine} ({_fx_location}) → FIX of #{issue} "
                f"(iteration {next_iteration}/{max_iter}) on branch {work.branch}"
            )
            click.echo(
                "  mode: HUMAN-ATTENDED interactive fix "
                "(migration leg 3 / Track B #486)"
            )
            click.echo(
                f"  assignment id: {assignment_id}  (fix_of={fix_of}, "
                f"work={work.assignment_id})"
            )
            if dry_run:
                click.echo("  (dry run — not launched)")
                click.echo(f"  would continue branch: {work.branch}")
                if not _is_local:
                    click.echo(
                        f"  remote worktree: $HOME/.coord/worktrees/{assignment_id}"
                        f" on {machine_obj.host} (branch: {work.branch})"
                    )
                click.echo(f"  would exec: {argv}")
                return

            fix_assignment = _AssignmentFx(
                machine_name=machine,
                repo_name=repo,
                issue_number=issue,
                issue_title=f"[fix-{next_iteration}] {issue_title}",
                briefing=effective_briefing,
                assignment_id=assignment_id,
                status="running",
                branch=work.branch,
                pr_url=work.pr_url,
                dispatched_at=_time.time(),
                type="work",
                review_of_assignment_id=work.assignment_id,
                review_iteration=next_iteration,
                model=resolved_model,
                provider_name="claude-pty",
            )
            _record_fx(assignment=fix_assignment, repo_github=repo_cfg.github)
            if _svc is None:
                _save_board_fx(_build_board_fx())

            if _is_local:
                try:
                    _wt_path, _ = _setup_wt_fx(
                        Path(fix_repo_path),
                        issue_number=issue,
                        issue_title=issue_title,
                        assignment_id=assignment_id,
                        default_branch=fix_default_branch,
                        existing_branch=work.branch,
                    )
                    worktree_path = str(_wt_path)
                except (_AgentGitErrorFx, OSError) as _wt_err:
                    click.echo(
                        f"  error: could not create fix worktree on branch "
                        f"{work.branch}: {_wt_err}",
                        err=True,
                    )
                    sys.exit(1)
                click.echo(f"  worktree: {worktree_path} (branch: {work.branch})")

                started_at = _time.time()
                exit_code = launch_human_attended_interactive(
                    argv,
                    effective_briefing,
                    assignment_id=assignment_id,
                    cwd=worktree_path,
                )
                if exit_code != 0:
                    click.echo(f"  claude exited with status {exit_code}", err=True)

                _sname = _tmux_name(assignment_id) if _tmux_avail() else None
                if _sname and _tmux_alive(_sname):
                    click.echo(
                        f"  session still running in tmux: {_sname}\n"
                        f"  reattach with:  coord reattach {assignment_id}"
                    )
                    sys.exit(0)

                try:
                    finalize_result = finalize_interactive_exit(
                        assignment_id=assignment_id,
                        repo_name=repo,
                        repo_github=repo_cfg.github,
                        issue_number=issue,
                        machine_name=machine,
                        worktree_path=worktree_path,
                        base_branch=fix_default_branch,
                        exit_code=exit_code,
                        started_at=started_at,
                        log_path=None,
                        repo_path=fix_repo_path,
                        artifact_paths=repo_cfg.artifact_paths,
                    )
                    if finalize_result.already_recorded:
                        click.echo(
                            "  result recorded via `coord report-result`; backstop "
                            "did not overwrite"
                        )
                    else:
                        click.echo(
                            f"  backstop: status={finalize_result.terminal_status} "
                            f"commits_ahead={finalize_result.commits_ahead}"
                        )
                        if not finalize_result.push_ok:
                            click.echo(
                                f"  warning: git push failed: {finalize_result.push_error}",
                                err=True,
                            )
                except Exception as exc:  # noqa: BLE001 — best-effort backstop
                    click.echo(
                        f"  warning: backstop failed to record fix exit: {exc}",
                        err=True,
                    )
                sys.exit(exit_code)

            # ── REMOTE FIX (Track B / #486) ───────────────────────────────
            # A remote worktree on the EXISTING branch (`-B <branch>
            # origin/<branch>` resets a dedicated local branch to the reviewed
            # work's branch); the session commits there; on exit the
            # coordinator pushes the commits back to origin + records the
            # completion (#486d) so the re-review fires.
            import shlex as _shlex_fx  # noqa: PLC0415

            _remote_wt = "$HOME/.coord/worktrees/" + assignment_id
            _rp_sh = (
                "$HOME/" + fix_repo_path[2:]
                if fix_repo_path.startswith("~/")
                else ("$HOME" if fix_repo_path == "~" else fix_repo_path)
            )
            _claude_args = _shlex_fx.join(list(argv)[1:])
            _br_q = _shlex_fx.quote(work.branch)
            _orig_ref = _shlex_fx.quote(f"origin/{work.branch}")
            _remote_cmd = (
                f"mkdir -p $HOME/.coord/worktrees"
                f" && cd {_rp_sh}"
                f" && git fetch origin --prune 2>/dev/null || true"
                f" && git worktree prune 2>/dev/null || true"
                f" && git worktree add -B {_br_q} {_remote_wt} {_orig_ref}"
                f" && cd {_remote_wt}"
                f" && COORD_ASSIGNMENT_ID={assignment_id} {argv[0]} {_claude_args}"
            )
            _tmux_host = TmuxHost(ssh_target=machine_obj.host)
            _sname = _tmux_name(assignment_id)
            click.echo(
                f"  remote worktree: $HOME/.coord/worktrees/{assignment_id}"
                f" on {machine_obj.host} (branch: {work.branch})"
            )

            if effective_briefing.strip():
                _hdr = (
                    "--- seeded briefing -- review below; "
                    "submit the pre-filled input in Claude to send ---"
                )
                _ftr = "-" * len(_hdr)
                _preview = f"\n{_hdr}\n{effective_briefing.rstrip()}\n{_ftr}\n\n"
                try:
                    os.write(sys.stdout.fileno(), _preview.encode("utf-8"))
                except OSError:
                    pass

            started_at = _time.time()
            _rc = _tmux_launch(
                argv,
                effective_briefing,
                _sname,
                cwd=None,
                host=_tmux_host,
                raw_shell_cmd=_remote_cmd,
            )
            if _rc is None:
                click.echo(
                    "  error: could not create remote tmux session on "
                    f"{machine_obj.host}",
                    err=True,
                )
                sys.exit(1)
            exit_code = _rc
            if exit_code != 0:
                click.echo(f"  claude exited with status {exit_code}", err=True)

            if _tmux_alive(_sname, host=_tmux_host):
                click.echo(
                    f"  session still running in remote tmux: {_sname}\n"
                    f"  reattach with:  ssh -t {machine_obj.host}"
                    f" tmux attach-session -t {_sname}\n"
                    "  (the fix is not pushed until the session ends and the "
                    "coordinator finalizes)"
                )
                sys.exit(0)

            # Remote finalize (#486d): push the fix commits to origin/<branch>,
            # record the completion, and clean up the remote worktree.
            try:
                _fr = finalize_remote_interactive_exit(
                    assignment_id=assignment_id,
                    repo_name=repo,
                    repo_github=repo_cfg.github,
                    issue_number=issue,
                    machine_name=machine,
                    ssh_target=machine_obj.host,
                    remote_worktree_sh=_remote_wt,
                    remote_repo_sh=_rp_sh,
                    branch=work.branch,
                    base_branch=fix_default_branch,
                    exit_code=exit_code,
                    started_at=started_at,
                    artifact_paths=repo_cfg.artifact_paths,
                )
                if _fr.already_recorded:
                    click.echo(
                        "  result recorded via `coord report-result`; remote "
                        "backstop did not overwrite"
                    )
                else:
                    click.echo(
                        f"  remote backstop: status={_fr.terminal_status} "
                        f"commits_ahead={_fr.commits_ahead} pushed={_fr.push_ok}"
                    )
                    if not _fr.push_ok:
                        click.echo(
                            f"  warning: remote push failed: {_fr.push_error}",
                            err=True,
                        )
                        click.echo(
                            f"  fix commits preserved in {_remote_wt} on "
                            f"{machine_obj.host} (worktree NOT removed)",
                            err=True,
                        )
            except Exception as exc:  # noqa: BLE001 — best-effort backstop
                click.echo(
                    f"  warning: remote backstop failed to record fix exit: {exc}",
                    err=True,
                )
            sys.exit(exit_code)

        # ── #563: --rework-of <work_aid|branch> ──────────────────────────
        # A human-attended REWORK of an existing branch — rebase, conflict
        # resolution, ad-hoc fixes after approval, etc.  Sibling of --fix-of
        # but WITHOUT the request-changes framing: the operator supplies the
        # briefing verbatim via --briefing.  Continues on the existing branch
        # (worktree + push-back identical to --fix-of), bumps review_iteration
        # so the reworked result is re-reviewed before merge.
        if rework_of is not None:
            from coord.agent import (  # noqa: PLC0415
                AssignmentSpec as _AssignmentSpecRw,
                _GitError as _AgentGitErrorRw,
                setup_interactive_worktree as _setup_wt_rw,
            )
            from coord.models import Assignment as _AssignmentRw  # noqa: PLC0415
            from coord.state import (  # noqa: PLC0415
                COORD_DIR as _COORD_DIR_RW,
                build_board as _build_board_rw,
                record_dispatched_assignment as _record_rw,
                save_board as _save_board_rw,
            )

            # Resolve branch: try to find a work assignment by ID first, then
            # fall back to treating the argument as a literal branch name.
            _rw_board = _interactive_board(_build_board_rw)
            _rw_work = _rw_board.find_by_id(rework_of)
            if _rw_work is not None:
                if not _rw_work.branch:
                    click.echo(
                        f"error: work assignment {rework_of} has no branch.",
                        err=True,
                    )
                    sys.exit(2)
                rw_branch = _rw_work.branch
                next_rw_iteration = (_rw_work.review_iteration or 0) + 1
                rw_work_id: str | None = _rw_work.assignment_id
            else:
                # Treat the argument as a branch name — useful when the
                # original assignment has aged off the board.
                rw_branch = rework_of
                # Look for any completed work on that branch to inherit
                # the iteration counter; default to 1 if none found.
                _branch_work = next(
                    (
                        a for a in _rw_board.completed
                        if a.branch == rw_branch and a.type in ("work", "plan")
                    ),
                    None,
                )
                next_rw_iteration = (
                    (_branch_work.review_iteration or 0) + 1
                    if _branch_work is not None
                    else 1
                )
                rw_work_id = (
                    _branch_work.assignment_id if _branch_work is not None else None
                )

            resolved_model = model if model else cfg.models.default
            assignment_id = _uuid.uuid4().hex[:12]
            if _is_local:
                rw_repo_path = str(
                    Path(machine_obj.repo_path(repo) or str(Path.cwd())).expanduser()
                )
            else:
                rw_repo_path = machine_obj.repo_path(repo) or f"~/src/{repo}"
            rw_default_branch = repo_cfg.default_branch or "main"

            os.environ["COORD_ASSIGNMENT_ID"] = assignment_id
            if _is_local:
                rw_report_reminder = (
                    f"[Coordinator rework assignment {assignment_id}] "
                    f"HUMAN-ATTENDED rework (iteration {next_rw_iteration}) "
                    f"on branch {rw_branch}. Before you exit, run "
                    f"`coord report-result --assignment {assignment_id} "
                    "--status done --summary <text>` so the coordinator "
                    "records the result and can re-review.\n\n"
                )
            else:
                rw_report_reminder = (
                    f"[Coordinator rework assignment {assignment_id}] "
                    f"HUMAN-ATTENDED rework (iteration {next_rw_iteration}) "
                    f"on branch {rw_branch}, running on a REMOTE machine. "
                    "Make your changes and COMMIT them. Before you exit, run "
                    f"`coord report-result --assignment {assignment_id} "
                    "--status done --summary <text>` — since #590 it routes to "
                    "the coordinator's shared board, so the result is recorded "
                    "from here. Do NOT run any `gh` commands. When you exit, the "
                    f"coordinator also pushes your commits to origin/{rw_branch} "
                    "and re-reviews.\n\n"
                )
            effective_briefing = rw_report_reminder + briefing

            spec = _AssignmentSpecRw(
                repo_name=repo,
                repo_path=rw_repo_path,
                issue_number=issue,
                issue_title=f"[rework-{next_rw_iteration}] {issue_title}",
                briefing=effective_briefing,
                model=resolved_model,
                type="work",
                provider="claude-pty",
            )
            argv = provider.build_command(spec, resolved_model=resolved_model)
            if not _is_local:
                argv = ["~/.local/bin/claude"] + list(argv)[1:]

            _rw_location = (
                "local TTY" if _is_local
                else f"{machine_obj.host} (remote tmux)"
            )
            _rw_max_iter = cfg.pipeline.max_review_iterations
            click.echo(
                f"{machine} ({_rw_location}) → REWORK of #{issue} "
                f"(iteration {next_rw_iteration}/{_rw_max_iter}) on branch {rw_branch}"
            )
            click.echo(
                "  mode: HUMAN-ATTENDED interactive rework (#563)"
            )
            click.echo(
                f"  assignment id: {assignment_id}  "
                f"(rework_of={rework_of!r}, branch={rw_branch})"
            )
            if dry_run:
                click.echo("  (dry run — not launched)")
                click.echo(f"  would continue branch: {rw_branch}")
                if not _is_local:
                    click.echo(
                        f"  remote worktree: $HOME/.coord/worktrees/{assignment_id}"
                        f" on {machine_obj.host} (branch: {rw_branch})"
                    )
                click.echo(f"  would exec: {argv}")
                return

            rw_assignment = _AssignmentRw(
                machine_name=machine,
                repo_name=repo,
                issue_number=issue,
                issue_title=f"[rework-{next_rw_iteration}] {issue_title}",
                briefing=effective_briefing,
                assignment_id=assignment_id,
                status="running",
                branch=rw_branch,
                pr_url=_rw_work.pr_url if _rw_work is not None else None,
                dispatched_at=_time.time(),
                type="work",
                review_of_assignment_id=rw_work_id,
                review_iteration=next_rw_iteration,
                model=resolved_model,
                provider_name="claude-pty",
            )
            _record_rw(assignment=rw_assignment, repo_github=repo_cfg.github)
            if _svc is None:
                _save_board_rw(_build_board_rw())

            if _is_local:
                try:
                    _wt_path, _ = _setup_wt_rw(
                        Path(rw_repo_path),
                        issue_number=issue,
                        issue_title=issue_title,
                        assignment_id=assignment_id,
                        default_branch=rw_default_branch,
                        existing_branch=rw_branch,
                    )
                    worktree_path = str(_wt_path)
                except (_AgentGitErrorRw, OSError) as _wt_err:
                    click.echo(
                        f"  error: could not create rework worktree on branch "
                        f"{rw_branch}: {_wt_err}",
                        err=True,
                    )
                    sys.exit(1)
                click.echo(f"  worktree: {worktree_path} (branch: {rw_branch})")

                started_at = _time.time()
                exit_code = launch_human_attended_interactive(
                    argv,
                    effective_briefing,
                    assignment_id=assignment_id,
                    cwd=worktree_path,
                )
                if exit_code != 0:
                    click.echo(f"  claude exited with status {exit_code}", err=True)

                _sname = _tmux_name(assignment_id) if _tmux_avail() else None
                if _sname and _tmux_alive(_sname):
                    click.echo(
                        f"  session still running in tmux: {_sname}\n"
                        f"  reattach with:  coord reattach {assignment_id}"
                    )
                    sys.exit(0)

                try:
                    finalize_result = finalize_interactive_exit(
                        assignment_id=assignment_id,
                        repo_name=repo,
                        repo_github=repo_cfg.github,
                        issue_number=issue,
                        machine_name=machine,
                        worktree_path=worktree_path,
                        base_branch=rw_default_branch,
                        exit_code=exit_code,
                        started_at=started_at,
                        log_path=None,
                        repo_path=rw_repo_path,
                        artifact_paths=repo_cfg.artifact_paths,
                    )
                    if finalize_result.already_recorded:
                        click.echo(
                            "  result recorded via `coord report-result`; backstop "
                            "did not overwrite"
                        )
                    else:
                        click.echo(
                            f"  backstop: status={finalize_result.terminal_status} "
                            f"commits_ahead={finalize_result.commits_ahead}"
                        )
                        if not finalize_result.push_ok:
                            click.echo(
                                f"  warning: git push failed: {finalize_result.push_error}",
                                err=True,
                            )
                except Exception as exc:  # noqa: BLE001 — best-effort backstop
                    click.echo(
                        f"  warning: backstop failed to record rework exit: {exc}",
                        err=True,
                    )
                sys.exit(exit_code)

            # ── REMOTE REWORK ─────────────────────────────────────────────
            import shlex as _shlex_rw  # noqa: PLC0415

            _remote_wt = "$HOME/.coord/worktrees/" + assignment_id
            _rp_sh = (
                "$HOME/" + rw_repo_path[2:]
                if rw_repo_path.startswith("~/")
                else ("$HOME" if rw_repo_path == "~" else rw_repo_path)
            )
            _claude_args = _shlex_rw.join(list(argv)[1:])
            _br_q = _shlex_rw.quote(rw_branch)
            _orig_ref = _shlex_rw.quote(f"origin/{rw_branch}")
            _remote_cmd = (
                f"mkdir -p $HOME/.coord/worktrees"
                f" && cd {_rp_sh}"
                f" && git fetch origin --prune 2>/dev/null || true"
                f" && git worktree prune 2>/dev/null || true"
                f" && git worktree add -B {_br_q} {_remote_wt} {_orig_ref}"
                f" && cd {_remote_wt}"
                f" && COORD_ASSIGNMENT_ID={assignment_id} {argv[0]} {_claude_args}"
            )
            _tmux_host = TmuxHost(ssh_target=machine_obj.host)
            _sname = _tmux_name(assignment_id)
            click.echo(
                f"  remote worktree: $HOME/.coord/worktrees/{assignment_id}"
                f" on {machine_obj.host} (branch: {rw_branch})"
            )

            if effective_briefing.strip():
                _hdr = (
                    "--- seeded briefing -- review below; "
                    "submit the pre-filled input in Claude to send ---"
                )
                _ftr = "-" * len(_hdr)
                _preview = f"\n{_hdr}\n{effective_briefing.rstrip()}\n{_ftr}\n\n"
                try:
                    os.write(sys.stdout.fileno(), _preview.encode("utf-8"))
                except OSError:
                    pass

            started_at = _time.time()
            _rc = _tmux_launch(
                argv,
                effective_briefing,
                _sname,
                cwd=None,
                host=_tmux_host,
                raw_shell_cmd=_remote_cmd,
            )
            if _rc is None:
                click.echo(
                    "  error: could not create remote tmux session on "
                    f"{machine_obj.host}",
                    err=True,
                )
                sys.exit(1)
            exit_code = _rc
            if exit_code != 0:
                click.echo(f"  claude exited with status {exit_code}", err=True)

            if _tmux_alive(_sname, host=_tmux_host):
                click.echo(
                    f"  session still running in remote tmux: {_sname}\n"
                    f"  reattach with:  ssh -t {machine_obj.host}"
                    f" tmux attach-session -t {_sname}\n"
                    "  (the rework is not pushed until the session ends and "
                    "the coordinator finalizes)"
                )
                sys.exit(0)

            # Remote finalize: push the rework commits to origin/<branch>,
            # record the completion, and clean up the remote worktree.
            try:
                _fr = finalize_remote_interactive_exit(
                    assignment_id=assignment_id,
                    repo_name=repo,
                    repo_github=repo_cfg.github,
                    issue_number=issue,
                    machine_name=machine,
                    ssh_target=machine_obj.host,
                    remote_worktree_sh=_remote_wt,
                    remote_repo_sh=_rp_sh,
                    branch=rw_branch,
                    base_branch=rw_default_branch,
                    exit_code=exit_code,
                    started_at=started_at,
                    artifact_paths=repo_cfg.artifact_paths,
                )
                if _fr.already_recorded:
                    click.echo(
                        "  result recorded via `coord report-result`; remote "
                        "backstop did not overwrite"
                    )
                else:
                    click.echo(
                        f"  remote backstop: status={_fr.terminal_status} "
                        f"commits_ahead={_fr.commits_ahead} pushed={_fr.push_ok}"
                    )
                    if not _fr.push_ok:
                        click.echo(
                            f"  warning: remote push failed: {_fr.push_error}",
                            err=True,
                        )
                        click.echo(
                            f"  rework commits preserved in {_remote_wt} on "
                            f"{machine_obj.host} (worktree NOT removed)",
                            err=True,
                        )
            except Exception as exc:  # noqa: BLE001 — best-effort backstop
                click.echo(
                    f"  warning: remote backstop failed to record rework exit: {exc}",
                    err=True,
                )
            sys.exit(exit_code)

        # ── Leg 3c (#517, #306): --merge-of <work_aid> ───────────────────
        # A human-attended interactive MERGE agent for a completed+approved
        # branch.  Merging is where the pipeline most often stalls — the branch
        # has gone stale against the default branch and needs a rebase, or there
        # are conflicts to resolve.  This continues the work branch in a worktree
        # (like --fix-of), proactively rebases it onto origin/<default_branch>
        # (#306), helps resolve conflicts, runs the tests, force-pushes with
        # --force-with-lease, then hands back to the operator to merge (TUI Go /
        # `coord merge`).  Self-contained and returns.  Local-only (Track B / #486).
        if merge_of is not None:
            from coord.agent import (  # noqa: PLC0415
                AssignmentSpec as _AssignmentSpecMg,
                _GitError as _AgentGitErrorMg,
                setup_interactive_worktree as _setup_wt_mg,
            )
            from coord.models import Assignment as _AssignmentMg  # noqa: PLC0415
            from coord.state import (  # noqa: PLC0415
                build_board as _build_board_mg,
                record_dispatched_assignment as _record_mg,
                save_board as _save_board_mg,
            )

            if not _is_local:
                click.echo(
                    "error: --merge-of is local-only for now; run it on the "
                    "machine that holds the checkout (remote interactive merge "
                    "is Track B / #486).",
                    err=True,
                )
                sys.exit(2)

            _mg_board = _interactive_board(_build_board_mg)
            work = _mg_board.find_by_id(merge_of)
            if work is None:
                click.echo(
                    f"error: --merge-of {merge_of}: no such assignment on the "
                    "board (use the work id from `coord status`).",
                    err=True,
                )
                sys.exit(2)
            if not work.branch:
                click.echo(
                    f"error: work assignment {merge_of} has no branch to merge.",
                    err=True,
                )
                sys.exit(2)

            resolved_model = model if model else cfg.models.default
            assignment_id = _uuid.uuid4().hex[:12]
            merge_repo_path = str(
                Path(machine_obj.repo_path(repo) or str(Path.cwd())).expanduser()
            )
            merge_target_branch = repo_cfg.default_branch or "main"
            _merge_test_cmd = None
            try:
                _merge_test_cmd = getattr(repo_cfg, "test_command", None)
            except Exception:  # noqa: BLE001
                _merge_test_cmd = None

            INTERACTIVE_MERGE_SYSTEM_PROMPT = (
                "You are a human-attended merge-prep agent dispatched by the "
                "coordinator. A human operator is at the keyboard with you. The "
                "branch has been reviewed/approved; your job is to get it cleanly "
                "rebased and ready to merge.\n\n"
                "Rules:\n"
                "- Stay on the worker's branch. NEVER push to the default branch "
                "directly.\n"
                "- Use `git push --force-with-lease` (NOT plain --force) after a "
                "rebase.\n"
                "- Resolve MECHANICAL conflicts (non-overlapping struct fields, "
                "list/import entries, separate functions) additively — keep both "
                "sides. For SEMANTIC conflicts (same logic changed two ways), do "
                "NOT guess: explain the conflict to the operator and let them "
                "decide.\n"
                "- Do NOT merge to the default branch yourself. After the rebase "
                "is clean, pushed, and tests pass, hand back to the operator: "
                "they merge via the TUI 'Go' button (or `coord merge`).\n\n"
                "Flow:\n"
                "1. `git fetch origin`.\n"
                "2. Rebase the branch onto `origin/<default_branch>`.\n"
                "3. Resolve conflicts (mechanical additively; semantic with the "
                "operator).\n"
                "4. Run the project's build/tests to confirm nothing broke.\n"
                "5. `git push --force-with-lease`.\n"
                "6. Tell the operator the branch is rebased + green and ready to "
                "merge.\n"
            )

            merge_briefing = (
                f"# Merge-prep assignment: {repo_cfg.github} #{issue}\n\n"
                f"**Issue:** {issue_title}\n"
                f"**Branch to merge:** `{work.branch}` "
                f"(worker: {work.machine_name or machine})\n"
                f"**Rebase onto:** `origin/{merge_target_branch}`\n"
                f"**Work assignment id:** `{merge_of}`\n"
                + (
                    f"**Test command:** `{_merge_test_cmd}`\n"
                    if _merge_test_cmd
                    else ""
                )
                + "\n## Your job\n\n"
                "This branch is approved. Fetch, rebase it onto "
                f"`origin/{merge_target_branch}` (#306 proactive rebase), resolve "
                "any conflicts (mechanical additively; semantic with the "
                "operator), run the tests, and `git push --force-with-lease`. "
                "Then tell the operator it's ready to merge — they press 'Go' in "
                "the TUI (or run `coord merge`). Do NOT merge to the default "
                "branch yourself.\n"
            )

            os.environ["COORD_ASSIGNMENT_ID"] = assignment_id
            report_reminder = (
                f"[Coordinator merge assignment {assignment_id}] HUMAN-ATTENDED "
                f"merge-prep on branch {work.branch} (rebasing onto "
                f"{merge_target_branch}). Before you exit, run `coord "
                f"report-result --assignment {assignment_id} --status done "
                "--summary <text>` (use --status blocked if a semantic conflict "
                "needs the operator).\n\n"
            )
            effective_briefing = report_reminder + merge_briefing

            spec = _AssignmentSpecMg(
                repo_name=repo,
                repo_path=merge_repo_path,
                issue_number=issue,
                issue_title=f"[merge] {issue_title}",
                briefing=effective_briefing,
                model=resolved_model,
                type="conflict-fix",
                provider="claude-pty",
            )
            # Full worker tool set (Read/Edit/Write/Bash) — rebasing and resolving
            # conflicts mutates the checkout.
            argv = provider.build_command(spec, resolved_model=resolved_model)

            click.echo(
                f"{machine} (local TTY) → MERGE-PREP of #{issue} "
                f"on branch {work.branch}: {issue_title}"
            )
            click.echo("  mode: HUMAN-ATTENDED interactive merge agent (leg 3c)")
            click.echo(
                f"  assignment id: {assignment_id}  (merge_of={merge_of}, "
                f"rebase onto origin/{merge_target_branch})"
            )
            if dry_run:
                click.echo("  (dry run — not launched)")
                click.echo(f"  would continue branch: {work.branch}")
                click.echo(f"  would exec: {argv}")
                return

            try:
                _wt_path, _ = _setup_wt_mg(
                    Path(merge_repo_path),
                    issue_number=issue,
                    issue_title=issue_title,
                    assignment_id=assignment_id,
                    default_branch=merge_target_branch,
                    existing_branch=work.branch,
                )
                worktree_path = str(_wt_path)
            except (_AgentGitErrorMg, OSError) as _wt_err:
                click.echo(
                    f"  error: could not create merge worktree on branch "
                    f"{work.branch}: {_wt_err}",
                    err=True,
                )
                sys.exit(1)
            click.echo(f"  worktree: {worktree_path} (branch: {work.branch})")

            merge_assignment = _AssignmentMg(
                machine_name=machine,
                repo_name=repo,
                issue_number=issue,
                issue_title=f"[merge] {issue_title}",
                briefing=effective_briefing,
                assignment_id=assignment_id,
                status="running",
                branch=work.branch,
                pr_url=work.pr_url,
                dispatched_at=_time.time(),
                type="conflict-fix",
                review_of_assignment_id=work.assignment_id,
                model=resolved_model,
                provider_name="claude-pty",
            )
            _record_mg(assignment=merge_assignment, repo_github=repo_cfg.github)
            if _svc is None:
                _save_board_mg(_build_board_mg())

            started_at = _time.time()
            exit_code = launch_human_attended_interactive(
                argv,
                effective_briefing,
                assignment_id=assignment_id,
                cwd=worktree_path,
            )
            if exit_code != 0:
                click.echo(f"  claude exited with status {exit_code}", err=True)

            _sname = _tmux_name(assignment_id) if _tmux_avail() else None
            if _sname and _tmux_alive(_sname):
                click.echo(
                    f"  session still running in tmux: {_sname}\n"
                    f"  reattach with:  coord reattach {assignment_id}"
                )
                sys.exit(0)

            try:
                finalize_result = finalize_interactive_exit(
                    assignment_id=assignment_id,
                    repo_name=repo,
                    repo_github=repo_cfg.github,
                    issue_number=issue,
                    machine_name=machine,
                    worktree_path=worktree_path,
                    base_branch=merge_target_branch,
                    exit_code=exit_code,
                    started_at=started_at,
                    log_path=None,
                    repo_path=merge_repo_path,
                )
                if finalize_result.already_recorded:
                    click.echo(
                        "  result recorded via `coord report-result`; backstop "
                        "did not overwrite"
                    )
                else:
                    click.echo(
                        f"  backstop: status={finalize_result.terminal_status} "
                        f"commits_ahead={finalize_result.commits_ahead}"
                    )
            except Exception as exc:  # noqa: BLE001 — best-effort backstop
                click.echo(
                    f"  warning: backstop failed to record merge exit: {exc}",
                    err=True,
                )
            sys.exit(exit_code)

        if _is_local:
            # Expand `~` — repo_paths in coordinator.yml use `~/src/...`,
            # and unlike the agent (which expands everywhere) this local
            # interactive launch passes the path straight to the child's cwd,
            # so a literal `~` would fail with "No such file or directory".
            # Local launch ⇒ local home.
            repo_path = str(
                Path(machine_obj.repo_path(repo) or str(Path.cwd())).expanduser()
            )
        else:
            # Remote: keep the raw path from coordinator.yml so the remote
            # shell can expand `~` itself.  Fall back to ~/src/<repo> when no
            # repo_path is configured (common for new machines).
            repo_path = machine_obj.repo_path(repo) or f"~/src/{repo}"

        effective_plan_only = plan_only or (
            cfg.dispatch.require_plan and not no_plan
        )
        repo_default_branch = repo_cfg.default_branch or "main"

        # ── Claim check.  Without this an operator can spawn two
        # interactive sessions on the same issue and both push competing
        # branches.  --force bypasses the check (mirrors the
        # claude -p path below).
        from coord.claim import claim_message, find_work_claim  # noqa: PLC0415
        from coord.state import (  # noqa: PLC0415
            build_board,
            record_dispatched,
            save_board,
        )

        board_check = build_board()
        if not force:
            claim = find_work_claim(issue, repo, repo_cfg.github, board_check)
            if claim is not None:
                click.echo(
                    f"  skipping: {claim_message(claim)}",
                    err=True,
                )
                sys.exit(1)

        from coord.agent import AssignmentSpec  # noqa: PLC0415
        from coord.models import Proposal  # noqa: PLC0415

        resolved_model = model if model else cfg.models.default
        assignment_id = _uuid.uuid4().hex[:12]

        spec = AssignmentSpec(
            repo_name=repo,
            repo_path=repo_path,
            issue_number=issue,
            issue_title=issue_title,
            briefing=briefing,
            model=resolved_model,
            type="plan" if effective_plan_only else "work",
            provider="claude-pty",
        )

        # Build a minimal Proposal — only the fields record_dispatched
        # consumes need to be set.  The actual record_dispatched call is
        # deferred until after the dry-run gate below so `--dry-run`
        # leaves no phantom "running" row in the DB.
        proposal = Proposal(
            id=0,
            machine_name=machine,
            repo_name=repo,
            issue_number=issue,
            issue_title=issue_title,
            rationale="manual --interactive dispatch (human-attended)",
            briefing=briefing,
            model=resolved_model,
            type="plan" if effective_plan_only else "work",
            required_gates=[],
        )

        argv = provider.build_command(spec, resolved_model=resolved_model)

        # For remote: replace the binary (argv[0]) with the absolute path
        # to claude on the remote machine.  A bare "claude" is not on the
        # SSH login PATH (#424/#425); ~/.local/bin/claude is the canonical
        # location installed by `claude` setup on Linux.
        _REMOTE_CLAUDE_BIN = "~/.local/bin/claude"
        if not _is_local:
            argv = [_REMOTE_CLAUDE_BIN] + list(argv)[1:]

        _location = "local TTY" if _is_local else f"{machine_obj.host} (remote tmux)"
        click.echo(f"{machine} ({_location}) → {repo} #{issue}: {issue_title}")
        click.echo("  mode: HUMAN-ATTENDED interactive launch (#437)")
        click.echo(f"  assignment id: {assignment_id}")
        click.echo(
            "  the briefing will be PRE-FILLED in the input box; "
            "press Enter to submit; Ctrl-C / `/exit` to end the session."
        )
        if dry_run:
            from coord.agent import _slugify as _slugify_dry  # noqa: PLC0415
            _dry_branch = f"issue-{issue}-{_slugify_dry(issue_title)}"
            click.echo("  (dry run — not launched)")
            click.echo(f"  would exec: {argv}")
            if _is_local:
                click.echo(
                    f"  cwd: worktree for {_dry_branch} "
                    f"(under ~/.coord/worktrees/<assignment_id>)"
                )
            else:
                _dry_wt = f"~/.coord/worktrees/{assignment_id}"
                click.echo(
                    f"  remote worktree: {_dry_wt} on {machine_obj.host}"
                    f" (branch: {_dry_branch})"
                )
            return

        if _is_local:
            # ── LOCAL PATH ────────────────────────────────────────────────
            # Byte-identical to the pre-#494 behaviour: create an isolated
            # worktree + feature branch locally via setup_interactive_worktree,
            # then attach the current terminal directly via
            # launch_human_attended_interactive.
            from coord.agent import (  # noqa: PLC0415
                _GitError as _AgentGitError,
                setup_interactive_worktree,
            )
            try:
                _wt_path, _interactive_branch = setup_interactive_worktree(
                    Path(repo_path),
                    issue_number=issue,
                    issue_title=issue_title,
                    assignment_id=assignment_id,
                    default_branch=repo_default_branch,
                )
                worktree_path = str(_wt_path)
            except (_AgentGitError, OSError) as _wt_err:
                click.echo(
                    f"  error: could not create worktree for interactive session: {_wt_err}",
                    err=True,
                )
                sys.exit(1)

            click.echo(f"  worktree: {worktree_path} (branch: {_interactive_branch})")

            # State mutations (DB row, env var, board write) ONLY on real
            # dispatch — never in dry-run.  Record up front so:
            #   * claim detection refuses a duplicate the second the human
            #     hits Enter on a parallel `coord assign --interactive`,
            #   * the board shows the in-flight interactive session,
            #   * the issue_store seam has a row to UPDATE on exit.
            record_dispatched(
                assignment_id=assignment_id,
                proposal=proposal,
                repo_github=repo_cfg.github,
                provider_name="claude-pty",
            )

            # #466: Inject the assignment id into the agent's process env so
            # the interactive Claude session can run
            # `coord report-result --assignment $COORD_ASSIGNMENT_ID …` to
            # report a structured result before exiting.  Also prepend a
            # short reminder to the briefing so the operator notices.
            os.environ["COORD_ASSIGNMENT_ID"] = assignment_id
            report_reminder = (
                f"[Coordinator assignment {assignment_id}] "
                "Before you exit, please run `coord report-result "
                f"--assignment {assignment_id} --status <done|blocked|"
                "already-implemented> [--verdict approve|request-changes] "
                "--summary <text>` so the coordinator records the result.\n\n"
            )
            effective_briefing = report_reminder + briefing

            # Update board metadata (round_number / board_initialized).
            # `record_dispatched` already wrote the assignment row, so the
            # build_board → save_board round-trip is a no-op for the
            # assignments table; the useful side-effect is board_meta.
            save_board(build_board())

            started_at = _time.time()
            # #487: pass assignment_id so the tmux path names the session
            # coord-<assignment_id>, enabling reattach after a TUI crash.
            exit_code = launch_human_attended_interactive(
                argv, effective_briefing, assignment_id=assignment_id, cwd=worktree_path,
            )
            if exit_code != 0:
                click.echo(f"  claude exited with status {exit_code}", err=True)

            # #487: if the tmux session is still alive the user just detached
            # (Ctrl-b d) or the TUI crashed.  Skip finalize — the session is
            # still running.  Tell the operator how to reattach and let them
            # close the session themselves (at which point coord report-result
            # or coord reattach will record the terminal state).
            _sname = _tmux_name(assignment_id) if _tmux_avail() else None
            if _sname and _tmux_alive(_sname):
                click.echo(
                    f"  session still running in tmux: {_sname}\n"
                    f"  reattach with:  coord reattach {assignment_id}\n"
                    f"  or from shell:  tmux attach-session -t {_sname}"
                )
                sys.exit(0)

            # #466 — git-floor backstop.  ALWAYS write a terminal state for
            # this assignment through the issue_store seam, regardless of
            # whether the agent typed `coord report-result` first.  The
            # finalizer respects an existing report (it checks the DB row's
            # status before clobbering).
            try:
                finalize_result = finalize_interactive_exit(
                    assignment_id=assignment_id,
                    repo_name=repo,
                    repo_github=repo_cfg.github,
                    issue_number=issue,
                    machine_name=machine,
                    worktree_path=worktree_path,
                    base_branch=repo_default_branch,
                    exit_code=exit_code,
                    started_at=started_at,
                    log_path=None,
                    repo_path=repo_path,
                    artifact_paths=repo_cfg.artifact_paths,
                )
                if finalize_result.already_recorded:
                    click.echo(
                        "  result already recorded via `coord report-result`; "
                        "backstop did not overwrite",
                    )
                else:
                    click.echo(
                        f"  backstop: status={finalize_result.terminal_status} "
                        f"commits_ahead={finalize_result.commits_ahead}"
                    )
                    if not finalize_result.push_ok:
                        click.echo(
                            f"  warning: git push failed: {finalize_result.push_error}",
                            err=True,
                        )
            except Exception as exc:  # noqa: BLE001 — best-effort backstop
                click.echo(
                    f"  warning: backstop failed to record completion: {exc}",
                    err=True,
                )

            sys.exit(exit_code)

        else:
            # ── REMOTE PATH (#494 / #486b) ────────────────────────────────
            # The target machine is not the local host.  We create a named
            # tmux session ON THE REMOTE machine that:
            #   1. cd's into the remote repo checkout,
            #   2. fetches origin + prunes worktrees,
            #   3. creates a feature-branch worktree at
            #      ~/.coord/worktrees/<assignment_id>,
            #   4. cd's into the worktree,
            #   5. launches claude with COORD_ASSIGNMENT_ID set inline.
            #
            # The local terminal ATTACHES to the remote tmux session so the
            # operator can drive it as if it were a local session.
            #
            # We use $HOME in the shell command (not ~) so that the paths
            # survive single-quote wrapping during remote transmission.
            # (~ inside single quotes is NOT expanded; $HOME inside a
            # tmux-run shell command IS expanded by the final shell.)
            import shlex as _shlex  # noqa: PLC0415

            from coord.agent import _slugify as _remote_slugify  # noqa: PLC0415

            # Mirror setup_interactive_worktree branch/worktree naming.
            _remote_branch = f"issue-{issue}-{_remote_slugify(issue_title)}"
            _remote_wt = "$HOME/.coord/worktrees/" + assignment_id

            # repo_path may be ~/src/repo or an absolute path; replace
            # leading ~/ with $HOME/ so the shell expands it correctly.
            _rp_sh = (
                "$HOME/" + repo_path[2:]
                if repo_path.startswith("~/")
                else ("$HOME" if repo_path == "~" else repo_path)
            )

            # Build the shell command the remote tmux session will run.
            # Tries fresh -b first (new branch from origin/default), falls
            # back to -B (force-reset) from origin/<branch> (retry case),
            # then from origin/<default> as a last resort.
            _claude_args = _shlex.join(list(argv)[1:])
            _remote_cmd = (
                f"mkdir -p $HOME/.coord/worktrees"
                f" && cd {_rp_sh}"
                f" && git fetch origin --prune 2>/dev/null || true"
                f" && git worktree prune 2>/dev/null || true"
                f" && (git worktree add -b {_remote_branch} {_remote_wt}"
                f" origin/{repo_default_branch} 2>/dev/null"
                f" || git worktree add -B {_remote_branch} {_remote_wt}"
                f" origin/{_remote_branch} 2>/dev/null"
                f" || git worktree add -B {_remote_branch} {_remote_wt}"
                f" origin/{repo_default_branch})"
                f" && cd {_remote_wt}"
                f" && COORD_ASSIGNMENT_ID={assignment_id}"
                f" {argv[0]} {_claude_args}"
            )

            _tmux_host = TmuxHost(ssh_target=machine_obj.host)
            _sname = _tmux_name(assignment_id)

            click.echo(
                f"  remote worktree: $HOME/.coord/worktrees/{assignment_id}"
                f" on {machine_obj.host} (branch: {_remote_branch})"
            )

            # State mutations (DB row, env var, board write) — same as local.
            record_dispatched(
                assignment_id=assignment_id,
                proposal=proposal,
                repo_github=repo_cfg.github,
                provider_name="claude-pty",
            )
            # #486d: record the remote feature branch on the assignment so a
            # later `coord reattach` can push it back (record_dispatched writes
            # from the Proposal, which carries no branch).
            try:
                from coord.state import get_connection as _gc_wb  # noqa: PLC0415
                _conn_wb = _gc_wb()
                _conn_wb.execute(
                    "UPDATE assignments SET branch=? WHERE assignment_id=?",
                    (_remote_branch, assignment_id),
                )
                _conn_wb.commit()
            except Exception:  # noqa: BLE001
                pass

            # Set COORD_ASSIGNMENT_ID in the local coordinator env as well
            # (for symmetry with local path; the remote process gets it
            # inline via the shell command).
            os.environ["COORD_ASSIGNMENT_ID"] = assignment_id
            report_reminder = (
                f"[Coordinator assignment {assignment_id}] "
                "Before you exit, please run `coord report-result "
                f"--assignment {assignment_id} --status <done|blocked|"
                "already-implemented> [--verdict approve|request-changes] "
                "--summary <text>` so the coordinator records the result.\n\n"
            )
            effective_briefing = report_reminder + briefing

            save_board(build_board())

            # Echo briefing to the LOCAL terminal before connecting to the
            # remote session, so the operator can read it before pressing
            # Enter (mirrors the tmux path in launch_human_attended_interactive).
            if effective_briefing.strip():
                _hdr = (
                    "--- seeded briefing -- review below; "
                    "submit the pre-filled input in Claude to send ---"
                )
                _ftr = "-" * len(_hdr)
                _preview = f"\n{_hdr}\n{effective_briefing.rstrip()}\n{_ftr}\n\n"
                try:
                    os.write(sys.stdout.fileno(), _preview.encode("utf-8"))
                except OSError:
                    pass

            # Launch the remote tmux session and attach to it.  Pass
            # raw_shell_cmd so _launch_via_tmux uses the verbatim shell
            # command (with $HOME paths and && operators) rather than
            # re-quoting argv through shlex.join.
            started_at = _time.time()
            _rc = _tmux_launch(
                argv,
                effective_briefing,
                _sname,
                cwd=None,
                host=_tmux_host,
                raw_shell_cmd=_remote_cmd,
            )
            if _rc is None:
                click.echo(
                    f"  error: could not create remote tmux session on"
                    f" {machine_obj.host}",
                    err=True,
                )
                sys.exit(1)
            exit_code = _rc
            if exit_code != 0:
                click.echo(f"  claude exited with status {exit_code}", err=True)

            # Check if the remote session is still alive (user detached).
            if _tmux_alive(_sname, host=_tmux_host):
                click.echo(
                    f"  session still running in remote tmux: {_sname}\n"
                    f"  reattach with:  coord reattach {assignment_id}\n"
                    "  (work commits are pushed when the session ends and the "
                    "coordinator finalizes)"
                )
                sys.exit(0)

            # Remote finalize (#486d): push the work commits to origin/<branch>,
            # record the completion (so the pipeline advances + a re-review can
            # fire), and clean up the remote worktree.  Mirrors the remote-FIX
            # path; the only difference is the branch is the fresh feature
            # branch this work session created, not an existing one.
            try:
                _fr = finalize_remote_interactive_exit(
                    assignment_id=assignment_id,
                    repo_name=repo,
                    repo_github=repo_cfg.github,
                    issue_number=issue,
                    machine_name=machine,
                    ssh_target=machine_obj.host,
                    remote_worktree_sh=_remote_wt,
                    remote_repo_sh=_rp_sh,
                    branch=_remote_branch,
                    base_branch=repo_default_branch,
                    exit_code=exit_code,
                    started_at=started_at,
                    artifact_paths=repo_cfg.artifact_paths,
                )
                if _fr.already_recorded:
                    click.echo(
                        "  result recorded via `coord report-result`; remote "
                        "backstop did not overwrite"
                    )
                else:
                    click.echo(
                        f"  remote backstop: status={_fr.terminal_status} "
                        f"commits_ahead={_fr.commits_ahead} pushed={_fr.push_ok}"
                    )
                    if not _fr.push_ok:
                        click.echo(
                            f"  warning: remote push failed: {_fr.push_error}",
                            err=True,
                        )
                        click.echo(
                            f"  work commits preserved in {_remote_wt} on "
                            f"{machine_obj.host} (worktree NOT removed)",
                            err=True,
                        )
            except Exception as exc:  # noqa: BLE001 — best-effort backstop
                click.echo(
                    f"  warning: remote backstop failed to record work exit: {exc}",
                    err=True,
                )
            sys.exit(exit_code)

    # Build a Proposal inline
    from coord.models import Proposal

    # Resolve model: --model flag → config default → None (let claude pick).
    resolved_model = model if model else cfg.models.default

    # Resolve required_gates: check issue labels against pipeline.labels config,
    # fall back to pipeline.default_gates.
    issue_labels: list[str] = [
        lbl.get("name", "") for lbl in (issue_data.get("labels") or [])
    ]
    resolved_gates: list[str] = list(cfg.pipeline.default_gates)
    for lbl in issue_labels:
        if lbl in cfg.pipeline.labels:
            resolved_gates = list(cfg.pipeline.labels[lbl])
            break

    # Determine effective plan-only mode.
    # --plan-only always wins; --no-plan overrides dispatch.require_plan;
    # otherwise dispatch.require_plan sets the default.
    effective_plan_only = plan_only or (cfg.dispatch.require_plan and not no_plan)

    proposal = Proposal(
        id=0,
        machine_name=machine,
        repo_name=repo,
        issue_number=issue,
        issue_title=issue_title,
        rationale="manual assignment via coord assign",
        briefing=briefing,
        model=resolved_model,
        type="plan" if effective_plan_only else "work",
        required_gates=resolved_gates,
    )

    click.echo(f"{machine} → {repo} #{issue}: {issue_title}")
    if effective_plan_only:
        if cfg.dispatch.require_plan and not plan_only:
            click.echo("  mode: plan-only (dispatch.require_plan=true; use --no-plan to override)")
        else:
            click.echo("  mode: plan-only (read-only, no worktree)")
    if resolved_model:
        click.echo(f"  model: {resolved_model}")

    if dry_run:
        click.echo("  (dry run — not dispatched)")
        return

    # Claim check
    from coord.claim import claim_message, find_work_claim

    board = build_board()
    if not force:
        claim = find_work_claim(issue, repo, repo_cfg.github, board)
        if claim is not None:
            click.echo(
                f"  skipping: {claim_message(claim)}",
                err=True,
            )
            sys.exit(1)

    # #267: dependency freshness check — same machinery `coord approve`
    # uses.  Default for `coord assign` is `--auto-pull` (the manual /
    # right-click dispatch path is a deliberate user action; we want it
    # to be safe by default).  `--no-pull` falls back to the briefing
    # addendum; `--skip-freshness` bypasses entirely.
    # #268: `relevant_repos` covers both transitive `depends_on` (build
    # deps) and direct `reference_repos` (context).
    pull_repos: list[str] = []
    if not skip_freshness:
        from coord import freshness as _fresh  # noqa: PLC0415
        from coord.network import fetch_repos  # noqa: PLC0415

        agent_repos = fetch_repos(machine_obj) or {}

        repos_needed = _fresh.relevant_repos(proposal, cfg)
        github_heads: dict[str, str | None] = {}
        for dep_name, _kind in repos_needed:
            dep_cfg = cfg.repo(dep_name)
            if dep_cfg is None:
                github_heads[dep_name] = None
                continue
            try:
                github_heads[dep_name] = github_ops.get_default_branch_head(
                    dep_cfg.github, dep_cfg.default_branch
                )
            except RuntimeError as e:
                click.echo(
                    f"  warning: could not get HEAD of {dep_cfg.github}: {e}",
                    err=True,
                )
                github_heads[dep_name] = None

        freshness = _fresh.dependency_freshness(
            proposal, cfg, agent_repos, github_heads
        )
        needs = _fresh.stale_or_dirty(freshness)
        if needs:
            for f in needs:
                click.echo(
                    f"  dependency {f.repo_name}: {f.state}"
                    + (f" ({f.error})" if f.error else ""),
                )
            if not no_pull:
                pull_repos = [f.repo_name for f in needs if f.state == _fresh.STALE]
                if pull_repos:
                    click.echo(f"  will pull on agent before worker: {pull_repos}")
            else:
                addendum = _fresh.format_briefing_addendum(freshness)
                if addendum:
                    proposal.briefing = (proposal.briefing or "") + addendum

    # Dispatch to agent server
    try:
        response = dispatch(
            proposal, cfg, pull_repos=pull_repos, fresh_branch=force,
        )
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
        provider_name=response.get("_provider_name"),
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

    # Mark session start on first dispatch of the session
    from coord.state import load_session, write_session_start
    session = load_session()
    if session is None or session.get("clean_shutdown", True):
        write_session_start()


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
@click.option(
    "--raw",
    is_flag=True,
    help="Dump the raw log (NDJSON for stream-json workers) instead of the human-readable rendering.",
)
def log(
    assignment_id: str,
    config_path: Path,
    follow: bool,
    machine_filter: str | None,
    force_local: bool,
    raw: bool,
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
        _log_local(assignment_id, follow, raw=raw)
        return

    _log_remote(target_machine, assignment_id, follow, raw=raw)


def _emit_log_text(text: str, *, raw: bool) -> None:
    """Print *text* either as-is (raw mode or plain-text log) or rendered."""
    if not text:
        return
    if raw:
        click.echo(text, nl=False)
        return

    from coord.worker_events import parse_event, render_event

    # Detect format heuristically: if the first non-blank, non-comment line
    # looks like JSON, treat the whole chunk as stream-json. Otherwise pass
    # through unchanged (plain-text fallback for legacy workers).
    is_json = False
    for raw_line in text.splitlines():
        stripped = raw_line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        is_json = stripped.startswith("{")
        break

    if not is_json:
        click.echo(text, nl=False)
        return

    turn_counter = [0]
    for raw_line in text.splitlines():
        stripped = raw_line.lstrip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            # Pass through the agent's header comment lines unchanged so the
            # user can still see argv and any pull-dep notes.
            click.echo(raw_line)
            continue
        event = parse_event(raw_line)
        if event is None:
            # Couldn't parse — show verbatim so nothing is silently dropped.
            click.echo(raw_line)
            continue
        rendered = render_event(event, turn_counter=turn_counter)
        if rendered is not None:
            click.echo(rendered)


def _log_local(assignment_id: str, follow: bool, *, raw: bool = False) -> None:
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
        from coord.worker_events import parse_event, render_event

        is_json: bool | None = None
        turn_counter = [0]

        with open(log_path) as f:
            while True:
                line = f.readline()
                if not line:
                    _time.sleep(0.3)
                    continue
                if raw:
                    click.echo(line, nl=False)
                    continue
                stripped = line.lstrip()
                if is_json is None:
                    if not stripped:
                        continue
                    if stripped.startswith("#"):
                        click.echo(line, nl=False)
                        continue
                    is_json = stripped.startswith("{")
                if not is_json:
                    click.echo(line, nl=False)
                    continue
                if stripped.startswith("#"):
                    click.echo(line, nl=False)
                    continue
                event = parse_event(line)
                if event is None:
                    click.echo(line, nl=False)
                    continue
                rendered = render_event(event, turn_counter=turn_counter)
                if rendered is not None:
                    click.echo(rendered)
    else:
        _emit_log_text(log_path.read_text(), raw=raw)


def _log_remote(machine, assignment_id: str, follow: bool, *, raw: bool = False) -> None:
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
    _emit_log_text(body.decode("utf-8", errors="replace"), raw=raw)
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
            _emit_log_text(body.decode("utf-8", errors="replace"), raw=raw)
            since += len(body)


@main.command("show-plan", help="Pretty-print the structured plan for a plan-only assignment.")
@click.argument("assignment_id")
def show_plan(assignment_id: str) -> None:
    from coord.plan_parser import WorkerPlan, parse_plan_from_log
    from coord.state import COORD_DIR, build_board, load_board, load_plans

    board = load_board() or build_board()
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


@main.command(help="Send a user message to a running worker mid-session.")
@click.argument("assignment_id")
@click.argument("text", nargs=-1, required=True)
@_CONFIG_OPTION
def inject(assignment_id: str, text: tuple[str, ...], config_path: Path) -> None:
    """Inject TEXT as a new user message into the running worker's session.

    The worker picks the message up at its next turn boundary — between
    tool calls, not mid-tool.  Useful for adding guidance to a worker
    that's going off the rails without having to stop + re-dispatch.
    """
    from coord.network import inject_message
    from coord.state import build_board, load_board

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

    message = " ".join(text).strip()
    if not message:
        click.echo("error: message text is empty", err=True)
        sys.exit(2)

    try:
        status, body = inject_message(machine, assignment_id, message)
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        click.echo(f"error: could not reach agent on {machine.name}: {e}", err=True)
        sys.exit(1)

    if status == 202:
        click.echo(
            f"Message delivered to {assignment.repo_name} #{assignment.issue_number} "
            f"on {machine.name}"
        )
    else:
        click.echo(
            f"error: agent rejected message (HTTP {status}): {body.get('error', body)}",
            err=True,
        )
        sys.exit(1)


@main.command(name="chat-continue", help="Continue a finished chat session with a new message.")
@click.argument("prior_assignment_id")
@click.argument("text", nargs=-1, required=True)
@_CONFIG_OPTION
def chat_continue(
    prior_assignment_id: str,
    text: tuple[str, ...],
    config_path: Path,
) -> None:
    """Re-dispatch a finished refinement assignment with TEXT as the next user turn.

    Looks up the claude session ID from the prior assignment and passes
    ``--resume <session_id>`` to the next worker so it loads the full
    conversation history before seeing TEXT as the next user message.

    Prints the new assignment ID on stdout so the TUI can bind to it.
    Does NOT post a GitHub briefing comment (chat turns are developer-side
    conversation, not issue activity).
    """
    from coord.db import get_connection
    from coord.dispatch import dispatch
    from coord.models import Proposal
    from coord.state import record_dispatched

    cfg = _load_config(config_path)

    conn = get_connection()
    row = conn.execute(
        "SELECT assignment_id, machine_name, repo_name, issue_number, issue_title, "
        "claude_session_id, type FROM assignments WHERE assignment_id=?",
        (prior_assignment_id,),
    ).fetchone()
    if row is None:
        click.echo(
            f"error: assignment {prior_assignment_id!r} not found in DB", err=True
        )
        sys.exit(1)

    # column may not exist on very old DBs that haven't migrated yet
    try:
        claude_session_id = row["claude_session_id"]
    except (IndexError, KeyError):
        claude_session_id = None

    machine_name = row["machine_name"]
    repo_name = row["repo_name"]
    issue_number = row["issue_number"]
    issue_title = row["issue_title"]
    message_text = " ".join(text).strip()

    # #316: preserve the chat type so the agent server uses the right system
    # prompt and tool restrictions on continuation.  The known chat types are
    # "refinement", "test-chat", and "new-issue-chat"; anything else falls
    # back to "refinement" (the original behaviour before type-preservation).
    _CHAT_TYPES = {"refinement", "test-chat", "new-issue-chat"}
    try:
        prior_type: str = row["type"] or "refinement"
    except (IndexError, KeyError):
        prior_type = "refinement"
    if prior_type not in _CHAT_TYPES:
        prior_type = "refinement"

    # #315: if the DB doesn't have the session_id yet, fetch it directly
    # from the agent's /status endpoint.  The notify cycle (typically every
    # 30s) is what syncs session_id from agent → DB; if the user types a
    # second chat message before notify catches up, the DB row is still
    # NULL even though the agent captured the session_id in memory.
    # Without this fallback every fast follow-up submit fails with
    # "no session ID captured" and the TUI's bind waits 30s and times out.
    if not claude_session_id:
        from coord.network import fetch_status  # noqa: PLC0415
        machine_for_status = next(
            (m for m in cfg.machines if m.name == machine_name), None,
        )
        if machine_for_status is not None:
            status_result = fetch_status(machine_for_status)
            if status_result.ok and status_result.data:
                # /status returns {"active": [...], "completed": [...]}
                # each entry is AgentAssignment.to_dict() with an `id` field
                for bucket in ("active", "completed"):
                    for entry in status_result.data.get(bucket, []):
                        if entry.get("id") == prior_assignment_id:
                            sid = entry.get("claude_session_id")
                            if isinstance(sid, str) and sid:
                                claude_session_id = sid
                                # Persist to DB so subsequent calls (and the
                                # coordinator's notify loop) don't re-fetch.
                                try:
                                    from coord.state import update_assignment_claude_session_id  # noqa: PLC0415
                                    update_assignment_claude_session_id(
                                        prior_assignment_id, sid,
                                    )
                                except Exception:  # noqa: BLE001
                                    pass
                            break
                    if claude_session_id:
                        break

    if not claude_session_id:
        click.echo(
            f"error: assignment {prior_assignment_id!r} has no session ID captured — "
            "agent has no session_id for this assignment (worker may not have "
            "emitted system.init, or the agent has restarted and forgotten it)",
            err=True,
        )
        sys.exit(1)

    repo_cfg = cfg.repo(repo_name)
    if repo_cfg is None:
        click.echo(f"error: repo {repo_name!r} not found in config", err=True)
        sys.exit(1)

    # Verify the target machine exists; warn but don't abort if missing
    # (the agent might still be reachable by name even if not in this config).
    machine = next((m for m in cfg.machines if m.name == machine_name), None)
    if machine is None:
        click.echo(
            f"warning: machine {machine_name!r} not in config — dispatch may fail",
            err=True,
        )

    # #315/#314/#316: use the type from the prior assignment so the agent
    # server uses the right system prompt and tool restrictions on continuation.
    # resume_session_id passes --resume so the full prior conversation is
    # loaded before the new user message is appended.
    proposal = Proposal(
        id=0,  # not inserted into proposals table; dummy value
        machine_name=machine_name,
        repo_name=repo_name,
        issue_number=issue_number,
        issue_title=issue_title,
        rationale="chat continuation",
        briefing=message_text,
        type=prior_type,
        resume_session_id=claude_session_id,
    )

    try:
        response = dispatch(proposal, cfg)
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: dispatch failed: {e}", err=True)
        sys.exit(1)

    assignment_id = response.get("id", "pending")

    # Record in coordinator DB so the board / TUI / notify see it.
    record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github=repo_cfg.github,
        provider_name=response.get("_provider_name"),
    )

    # Print the new assignment ID on stdout so callers (e.g. TUI) can bind.
    click.echo(assignment_id)


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


@main.command(
    "report-result",
    help=(
        "Report the outcome of an interactive session through the "
        "coordinator's issue_store seam (#466). "
        "REQUIRED for review sessions where the verdict can only come "
        "from the agent."
    ),
)
@click.option(
    "--assignment", "assignment_id_opt", default=None,
    help="The assignment id (defaults to $COORD_ASSIGNMENT_ID).",
)
@click.option(
    "--status",
    type=click.Choice(["done", "blocked", "already-implemented"]),
    required=True,
    help=(
        "Terminal result: `done` = work landed; `blocked` = cannot proceed; "
        "`already-implemented` = nothing to do (advisory)."
    ),
)
@click.option(
    "--verdict",
    type=click.Choice(["approve", "request-changes"]),
    default=None,
    help=(
        "Review verdict — only meaningful for review sessions where no "
        "commits are pushed. Recorded so the merge-gate sees the same "
        "field a claude-p reviewer would have populated."
    ),
)
@click.option(
    "--summary", default="",
    help="One-paragraph summary posted on the issue under the result.",
)
@click.option(
    "--body-file", "body_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help=(
        "Path to a file with the FULL findings body (markdown). For a REVIEW "
        "session, write your complete review here and pass this — it is persisted "
        "on the assignment AND posted to the issue under a machine-parseable "
        "marker, so the fix worker is briefed with the actual findings (from any "
        "machine, via the GitHub message bus), not just the one-line --summary. "
        "REQUIRED with `--verdict request-changes` (#580)."
    ),
)
@click.option(
    "--body", "body_inline", default=None,
    help=(
        "Inline alternative to --body-file (the full findings body as a string, "
        "e.g. --body \"$(cat findings.md)\"). One of --body/--body-file is "
        "required with `--verdict request-changes`."
    ),
)
@_CONFIG_OPTION
def report_result(
    assignment_id_opt: str | None,
    status: str,
    verdict: str | None,
    summary: str,
    body_file: str | None,
    body_inline: str | None,
    config_path: Path,
) -> None:
    """``coord report-result --assignment <id> --status <s> [--verdict <v>] --summary <text>``

    The single coordinator-mediated command an interactive Claude
    session may invoke before it exits.  Writes the outcome through the
    :mod:`coord.issue_store` seam (same path the git-floor backstop
    uses), so the GitHub message bus and the local DB see a
    structurally-identical completion regardless of which mechanism
    produced it.
    """
    import os as _os  # noqa: PLC0415

    from coord import issue_store  # noqa: PLC0415
    from coord.client import resolve_board_service  # noqa: PLC0415

    assignment_id = assignment_id_opt or _os.environ.get("COORD_ASSIGNMENT_ID")
    if not assignment_id:
        click.echo(
            "error: --assignment is required (or set $COORD_ASSIGNMENT_ID)",
            err=True,
        )
        sys.exit(2)

    repo_github: str | None = None
    repo_name: str | None = None
    machine_name: str | None = None
    issue_number: int | None = None
    branch: str | None = None

    svc = resolve_board_service()
    if svc is not None:
        # Thin client (#590): no local DB/config — resolve the assignment's
        # identity from the daemon's board payload (the assignments rows carry
        # repo_github), then let issue_store.post_result route the write back to
        # the daemon's shared DB.  This is what lets a remote interactive
        # session self-report instead of the old "do NOT run report-result"
        # workaround.
        from coord.client import fetch_board_payload  # noqa: PLC0415

        try:
            payload = fetch_board_payload(svc)
        except Exception as exc:  # noqa: BLE001
            click.echo(
                f"error: could not reach board service {svc.url}: {exc}", err=True
            )
            sys.exit(1)
        row = next(
            (
                a
                for a in payload.get("assignments", [])
                if a.get("assignment_id") == assignment_id
            ),
            None,
        )
        if row is not None:
            repo_github = row.get("repo_github")
            repo_name = row.get("repo_name")
            machine_name = row.get("machine_name")
            issue_number = row.get("issue_number")
            branch = row.get("branch")
    else:
        from coord.state import build_board, load_dispatched  # noqa: PLC0415

        cfg = _load_config(config_path)

        # Look up the assignment metadata.  Prefer the dispatched ledger
        # because it always has repo_github, then fall back to the live
        # board for in-flight rows that haven't been queried elsewhere.
        record = next(
            (r for r in load_dispatched() if r.get("assignment_id") == assignment_id),
            None,
        )
        if record is not None:
            repo_github = record.get("repo_github")
            repo_name = record.get("repo_name")
            machine_name = record.get("machine_name")
            issue_number = record.get("issue_number")

        board = build_board()
        assignment_obj = board.find_by_id(assignment_id)
        if assignment_obj is not None:
            repo_name = repo_name or assignment_obj.repo_name
            machine_name = machine_name or assignment_obj.machine_name
            issue_number = issue_number or assignment_obj.issue_number
            branch = assignment_obj.branch
            if repo_github is None:
                repo_cfg = cfg.repo(assignment_obj.repo_name)
                if repo_cfg is not None:
                    repo_github = repo_cfg.github

        # Final fallback: if a config repo matches the recorded repo_name,
        # use its github slug.
        if repo_github is None and repo_name is not None:
            repo_cfg = cfg.repo(repo_name)
            if repo_cfg is not None:
                repo_github = repo_cfg.github

    if not (repo_github and repo_name and machine_name and issue_number):
        click.echo(
            f"error: could not resolve assignment {assignment_id!r} from "
            "board/dispatched ledger; pass --assignment with a known id "
            "or run from the originating coordinator machine.",
            err=True,
        )
        sys.exit(1)

    findings_body: str | None = None
    if body_file:
        try:
            findings_body = Path(body_file).read_text(encoding="utf-8").strip() or None
        except OSError as exc:
            click.echo(
                f"warning: could not read --body-file {body_file!r}: {exc}",
                err=True,
            )
    if findings_body is None and body_inline and body_inline.strip():
        findings_body = body_inline.strip()

    # #580: a request-changes verdict MUST carry the reviewer's findings.
    # Recording it with only a one-line --summary silently discards the
    # objections, so the iteration-N+1 fix agent gets dispatched with nothing
    # to fix. Require the body (file or inline) and fail loudly otherwise.
    if verdict == "request-changes" and not findings_body:
        click.echo(
            "error: --verdict request-changes requires the review body — pass "
            "--body-file <path> (or --body \"<text>\") with your full findings "
            "(every blocking item, file:line). The one-line --summary is not "
            "enough; it's what the fix worker is briefed with.\n"
            "  Write your findings to a file and re-run, e.g.:\n"
            f"  coord report-result --assignment {assignment_id} --status done "
            "--verdict request-changes --summary <one-line> "
            f"--body-file /tmp/review-{assignment_id}.md",
            err=True,
        )
        sys.exit(2)

    record_obj = issue_store.ResultRecord(
        assignment_id=assignment_id,
        machine_name=machine_name,
        repo_name=repo_name,
        repo_github=repo_github,
        issue_number=int(issue_number),
        status=status,  # type: ignore[arg-type]
        verdict=verdict,  # type: ignore[arg-type]
        summary=summary,
        branch=branch,
        findings_body=findings_body,
    )
    try:
        outcome = issue_store.post_result(record_obj)
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)

    click.echo(
        f"result recorded: status={outcome.status} event={outcome.event} "
        f"posted_to_github={outcome.posted}"
    )
    if outcome.error:
        click.echo(f"  github post warning: {outcome.error}", err=True)


def _maybe_reconcile_branch(
    assignment, repo_dir, *, original_error: str, config,
):
    """When `git checkout <db_branch>` fails, try to learn the PR's actual
    head ref from GitHub and reconcile the DB.

    Returns the new branch name when reconciliation succeeded (DB
    updated + the reconciled branch verified on origin), or `None` when no
    PR is associated, the gh call failed, the head ref matches what we
    already had, or the reconciled ref is missing on origin.  The caller
    falls back to the original error in those cases.
    """
    from coord.db import get_connection

    # Need a PR number to look up the head ref.  Pull it from the
    # merge_queue entry for this assignment.
    aid = assignment.assignment_id
    if not aid:
        return None
    conn = get_connection()
    row = conn.execute(
        "SELECT pr_number, repo_github FROM merge_queue "
        "WHERE assignment_id=?",
        (aid,),
    ).fetchone()
    if row is None:
        return None
    pr_number = row["pr_number"]
    repo_github = row["repo_github"]
    if pr_number is None or not repo_github:
        return None

    # Fetch the PR's actual head ref from GitHub.  Returns the real
    # branch name even when the DB has a stale slug.
    try:
        gh = subprocess.run(
            [
                "gh", "pr", "view", str(pr_number),
                "--repo", repo_github,
                "--json", "headRefName",
                "--jq", ".headRefName",
            ],
            check=True, capture_output=True, text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    real_branch = gh.stdout.strip()
    if not real_branch:
        return None
    if real_branch == assignment.branch:
        # The PR DOES point at the DB-recorded branch; checkout failed
        # for some other reason (local-only clone, network, etc.).
        # Don't pretend we fixed it.
        return None

    # Validate the reconciled branch exists on origin before writing it to
    # the DB.  #561: this MUST be non-mutating — never `git checkout` in the
    # base checkout (it doubles as the live editable coordinator source).
    # `git fetch origin` already ran in the caller, so origin/<branch> is
    # current; a rev-parse verify confirms it without moving HEAD.
    try:
        subprocess.run(
            ["git", "rev-parse", "--verify", f"origin/{real_branch}"],
            cwd=str(repo_dir), check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError:
        return None

    # Persist the reconciled branch on both tables so future runs of
    # coord test / coord merge / TUI etc. all see the right value.
    conn.execute(
        "UPDATE assignments SET branch=? WHERE assignment_id=?",
        (real_branch, aid),
    )
    conn.execute(
        "UPDATE merge_queue SET branch=? WHERE assignment_id=?",
        (real_branch, aid),
    )
    conn.commit()

    # Mute the unused 'original_error' / 'config' params — they're
    # there for future use (e.g. logging context, post-back to GitHub).
    _ = original_error
    _ = config
    return real_branch


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

    # Determine escalated model for the retry.
    original_model = assignment.model or cfg.models.default
    escalated = cfg.models.next_model(original_model)
    if escalated != original_model:
        click.echo(f"  escalating model: {original_model} → {escalated}")

    result = _reassign(assignment, board, cfg, model=escalated)
    if result is None:
        click.echo("error: no available machine to retry on", err=True)
        sys.exit(1)

    save_board(board)
    click.echo(
        f"Retried: {result.machine_name} → {result.repo_name} "
        f"#{result.issue_number} (assignment {result.assignment_id})"
    )


@main.command(
    "pull-artifact",
    help=(
        "Pull built artifacts from an agent machine after a work assignment "
        "completes.  The agent stashes files matching `artifact_paths` globs "
        "configured in coordinator.yml before the worktree is removed.  This "
        "command queries the manifest and rsyncs the files locally.\n\n"
        "Requires passwordless SSH access to the agent host (see "
        "docs/AGENT_OPERATIONS.md for setup)."
    ),
)
@click.argument("assignment_id")
@click.option(
    "--into",
    "dest_path",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Local directory to rsync artifacts into.  "
        "Defaults to ~/.coord/artifacts/<repo>/<branch>/ (stable per-branch "
        "path; pulling the same branch twice overwrites the same location)."
    ),
)
@_CONFIG_OPTION
def pull_artifact(assignment_id: str, dest_path: Path | None, config_path: Path) -> None:
    """Rsync build artifacts from the agent machine that ran ASSIGNMENT_ID."""
    from coord.agent import _sanitize_branch, _slugify
    from coord.client import resolve_board_service

    cfg = _load_config(config_path)

    # ── Look up (machine, repo, branch) ──────────────────────────────────
    # #601: a thin client's local DB is retired, so resolve from the daemon's
    # board when board_service is set (the artifact itself is still pulled from
    # the agent host below — that works from any machine over Tailscale).
    svc = resolve_board_service()
    if svc is not None:
        from coord.client import fetch_board_payload  # noqa: PLC0415

        try:
            payload = fetch_board_payload(svc)
        except Exception as exc:  # noqa: BLE001
            click.echo(
                f"error: could not reach board service {svc.url}: {exc}", err=True
            )
            sys.exit(1)
        row = next(
            (
                a
                for a in payload.get("assignments", [])
                if a.get("assignment_id") == assignment_id
            ),
            None,
        )
    else:
        from coord.db import get_connection  # noqa: PLC0415

        conn = get_connection()
        row = conn.execute(
            "SELECT machine_name, repo_name, branch, issue_number, issue_title "
            "FROM assignments WHERE assignment_id = ?",
            (assignment_id,),
        ).fetchone()

    if row is None:
        click.echo(f"error: assignment {assignment_id!r} not found in database", err=True)
        sys.exit(1)

    machine_name: str = row["machine_name"]
    repo_name: str = row["repo_name"]
    branch: str | None = row["branch"]
    issue_number: int = row["issue_number"]
    issue_title: str = row["issue_title"]

    machine = next((m for m in cfg.machines if m.name == machine_name), None)
    if machine is None:
        click.echo(
            f"error: machine {machine_name!r} (from DB) not found in coordinator.yml",
            err=True,
        )
        sys.exit(1)

    # If branch is not yet recorded in the DB (notify hasn't run yet),
    # fall back to the deterministic name derived from issue_number + title.
    if not branch:
        branch = f"issue-{issue_number}-{_slugify(issue_title)}"

    sanitized = _sanitize_branch(branch)

    # ── Query the manifest endpoint ───────────────────────────────────────
    url = f"http://{machine.host}:{AGENT_PORT}/artifact/{repo_name}/{sanitized}"
    try:
        resp = httpx.get(url, timeout=10)
    except (httpx.HTTPError, httpx.TimeoutException, OSError) as e:
        click.echo(
            f"error: could not reach agent on {machine.host}:{AGENT_PORT}: {e}",
            err=True,
        )
        sys.exit(1)

    if resp.status_code == 404:
        click.echo(
            f"error: no artifacts found for assignment {assignment_id!r} "
            f"(repo={repo_name!r}, branch={sanitized!r}) on {machine.name}.\n"
            "Possible causes: stash has been GC'd (default TTL 3 days), "
            "the build did not match any artifact_paths globs, "
            "or artifact_paths is not configured for this repo.",
            err=True,
        )
        sys.exit(1)

    if resp.status_code != 200:
        click.echo(
            f"error: agent returned HTTP {resp.status_code}: {resp.text[:200]}",
            err=True,
        )
        sys.exit(1)

    manifest = resp.json()
    files = manifest.get("files", [])
    if not files:
        click.echo(
            f"No artifact files in stash for {assignment_id!r}. "
            "The build may have produced no files matching artifact_paths.",
            err=True,
        )
        sys.exit(1)

    total_bytes = manifest.get("total_bytes", 0)
    built_by = manifest.get("built_by_assignment_id") or assignment_id
    click.echo(
        f"Found {len(files)} artifact(s) ({total_bytes:,} bytes) "
        f"on {machine.name} (built by {built_by}):"
    )
    for f in files:
        click.echo(f"  {f['name']}  ({f['size']:,} bytes)")

    # ── Determine destination ─────────────────────────────────────────────
    if dest_path is None:
        # Default to a stable per-branch location so pulling the same branch
        # twice overwrites the same local path rather than creating new
        # directories each time.
        dest_path = Path.home() / ".coord" / "artifacts" / repo_name / sanitized
    dest_path.mkdir(parents=True, exist_ok=True)

    # ── Local-machine short-circuit ───────────────────────────────────────
    # When the artifact was built on the machine running this command (e.g.
    # the coordinator/TUI host), the agent already stashed the files locally
    # at ~/.coord/artifacts/<repo>/<branch>/ — there is nothing to fetch, and
    # rsync-over-ssh to our own hostname FAILS ("Permission denied" — no
    # self-ssh key), which surfaced as a meaningless pull error in the TUI.
    # Copy locally if the destination differs; otherwise it is a no-op.
    local_hostname = socket.gethostname().split(".")[0].lower()
    is_local = (
        machine.name.lower() == local_hostname
        or machine.host.split(".")[0].lower() == local_hostname
    )
    if is_local:
        src_dir = Path.home() / ".coord" / "artifacts" / repo_name / sanitized
        if src_dir.resolve() == dest_path.resolve():
            click.echo(f"\nArtifacts already local at: {dest_path}")
            return
        click.echo(f"\nCopying local artifacts {src_dir}/ → {dest_path}/")
        for item in src_dir.iterdir():
            if item.name == ".assignment_id":
                continue
            target = dest_path / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
        click.echo(f"\nArtifacts saved to: {dest_path}")
        return

    # ── rsync ─────────────────────────────────────────────────────────────
    remote = f"{machine.host}:~/.coord/artifacts/{repo_name}/{sanitized}/"
    cmd = [
        "rsync", "-az", "--info=progress2",
        # BatchMode=yes: ssh must NEVER prompt.  When this runs under the TUI,
        # an ssh passphrase/password/changed-host-key prompt opens /dev/tty
        # directly — bypassing the nulled stdin — and hijacks the TUI's
        # terminal (screen corruption, unresponsive to 'q').  BatchMode makes
        # ssh fail fast instead; the TUI captures stderr and toasts it.
        # accept-new: auto-accept a *new* host key on first contact so the
        # pull stays non-interactive on a fresh agent machine (safe on
        # Tailscale, where the network is already authenticated).
        "-e", "ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new",
        "--exclude=.assignment_id",
        remote,
        str(dest_path) + "/",
    ]
    click.echo(f"\nRsyncing {remote} → {dest_path}/")
    # start_new_session + stdin=DEVNULL: belt-and-braces so no descendant
    # (ssh) can claim the controlling terminal even if BatchMode is somehow
    # bypassed — see the TTY-hijack note on the rsync command above.
    result = subprocess.run(cmd, stdin=subprocess.DEVNULL, start_new_session=True)

    if result.returncode != 0:
        click.echo(
            f"error: rsync exited {result.returncode}. "
            "Ensure passwordless SSH is set up between coordinator and agent "
            "(see docs/AGENT_OPERATIONS.md).",
            err=True,
        )
        sys.exit(1)

    click.echo(f"\nArtifacts saved to: {dest_path}")


@main.command(
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
    from coord.state import COORD_DIR, build_board, load_board, save_board

    cfg = _load_config(config_path)
    board = load_board() or build_board()

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
        save_board(board)

    for a in actions:
        click.echo(f"{a.kind}: {a.detail}")

    if not dispatched:
        # Distinguish clean outcomes (approve / already-merged-or-closed) from
        # genuine failure modes.
        if any(a.kind in ("approved", "terminal_skip") for a in actions):
            sys.exit(0)
        sys.exit(1)


@main.command(help="Sync open issues from GitHub into the local SQLite cache.")
@_CONFIG_OPTION
@click.option("--quiet", "-q", is_flag=True, help="Suppress per-repo output.")
def sync(config_path: Path, quiet: bool) -> None:
    """Fetch open issues for every configured repo and write them to the local
    ``issues`` table in ``~/.coord/coord.db``.

    The TUI board reads from this table to show the full backlog under
    Pending.  Run this manually, call it from a cron job, or press 'r' in
    the TUI which triggers it automatically alongside the data refresh.
    """
    from coord import github_ops
    from coord.state import upsert_open_issues

    cfg = _load_config(config_path)
    total = 0
    for repo in cfg.repos:
        try:
            issues = github_ops.get_open_issues(repo.github)
            upsert_open_issues(repo.name, issues)
            if not quiet:
                click.echo(f"  {repo.name}: {len(issues)} open issue(s)")
            total += len(issues)
        except Exception as e:  # noqa: BLE001
            click.echo(f"  {repo.name}: sync failed — {e}", err=True)
    if not quiet:
        click.echo(f"synced {total} open issue(s) across {len(cfg.repos)} repo(s)")


def _apply_label_change(
    repo: str,
    issue: int,
    config_path: Path,
    *,
    add: set[str],
    remove_if_present: set[str],
    success_message: str,
    no_op_message: str | None = None,
) -> None:
    """Shared backbone for the four label-change commands (#260/#261/#266).

    Resolves *repo* via ``coordinator.yml``, fetches the issue's current
    labels via ``gh issue view``, computes the post-edit label set, runs
    ``gh issue edit`` with only the labels that are actually present
    (``--remove-label`` errors on unknown repo labels), then writes the
    new label set to the local ``issues`` cache so the TUI's next data
    refresh tick reflects the change without waiting for the 5-minute
    ``coord sync`` throttle.

    ``no_op_message`` (optional) is echoed when there are no add/remove
    ops to perform — used by ``coord backlog`` to say "already in
    Backlog" instead of running a no-op ``gh issue edit``.
    """
    import subprocess as _sp  # noqa: PLC0415
    import json as _json  # noqa: PLC0415

    from coord.state import update_issue_labels  # noqa: PLC0415

    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r} (not in coordinator.yml)", err=True)
        sys.exit(1)
    slug = repo_entry.github

    try:
        view = _sp.run(
            ["gh", "issue", "view", str(issue), "--repo", slug, "--json", "labels"],
            capture_output=True, text=True, timeout=15,
        )
    except (_sp.TimeoutExpired, OSError) as e:
        click.echo(f"error: failed to run gh view: {e}", err=True)
        sys.exit(1)
    if view.returncode != 0:
        click.echo(f"gh failed: {view.stderr.strip()}", err=True)
        sys.exit(1)
    try:
        current = {lbl.get("name", "") for lbl in _json.loads(view.stdout).get("labels", [])}
    except _json.JSONDecodeError as e:
        click.echo(f"could not parse gh view output: {e}", err=True)
        sys.exit(1)

    to_add = add - current
    to_remove = remove_if_present & current
    if not to_add and not to_remove:
        if no_op_message is not None:
            click.echo(no_op_message)
        else:
            click.echo(success_message)
        return

    args = ["gh", "issue", "edit", str(issue), "--repo", slug]
    for lbl in sorted(to_add):
        args.extend(["--add-label", lbl])
    for lbl in sorted(to_remove):
        args.extend(["--remove-label", lbl])

    try:
        result = _sp.run(args, capture_output=True, text=True, timeout=15)
    except (_sp.TimeoutExpired, OSError) as e:
        click.echo(f"error: failed to run gh edit: {e}", err=True)
        sys.exit(1)
    if result.returncode != 0:
        click.echo(f"gh failed: {result.stderr.strip()}", err=True)
        sys.exit(1)

    new_labels = sorted((current - to_remove) | to_add)
    update_issue_labels(repo, issue, new_labels)

    click.echo(success_message)


@main.command(
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


@main.command(
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


@main.command(
    "refine-chat",
    help=(
        "#264: dispatch a refinement-chat session for an issue.\n\n"
        "Seeds a `type=\"refinement\"` `claude -p` worker with the issue "
        "body + recent comments + the repo's CLAUDE.md + a bounded file-tree "
        "snapshot, then prints the new assignment id to stdout.  The TUI "
        "captures the id and opens a ChatController overlay bound to it; "
        "developer-typed turns flow via `POST /inject/{id}` and assistant "
        "replies stream back via the existing SSE watch.\n\n"
        "Read-only — refinement workers have only the `Read` tool; they "
        "cannot mutate the repo or talk to GitHub.  The Done button in the "
        "TUI calls `coord ready` to flip `status:refining` → `status:ready` "
        "on session end.\n\n"
        "REPO is the local repo name from coordinator.yml; ISSUE is the GH "
        "issue number."
    ),
)
@click.argument("repo")
@click.argument("issue", type=int)
@click.option(
    "--machine",
    default=None,
    help="Override machine selection (default: first reachable machine that lists the repo).",
)
@_CONFIG_OPTION
def refine_chat(repo: str, issue: int, machine: str | None, config_path: Path) -> None:
    cfg = _load_config(config_path)
    repo_cfg = cfg.repo(repo)
    if repo_cfg is None:
        click.echo(
            f"error: repo {repo!r} not in coordinator.yml "
            f"(have: {[r.name for r in cfg.repos]})",
            err=True,
        )
        sys.exit(2)

    from coord.refine_chat import dispatch_refinement
    try:
        assignment_id, _picked_machine = dispatch_refinement(
            cfg=cfg,
            repo_cfg=repo_cfg,
            repo=repo,
            issue_number=issue,
            machine_override=machine,
        )
    except RuntimeError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    # Also flip status:backlog → status:refining so the lifecycle view
    # shows the issue is being actively refined.  Best-effort; the chat
    # session itself is the actual refinement work.
    _apply_label_change(
        repo, issue, config_path,
        add={"status:refining"},
        remove_if_present={"status:ready", "status:backlog"},
        success_message="",  # no echo — keep stdout clean for the TUI
    )

    # Print the assignment_id as the LAST line on stdout so callers (the
    # TUI) can capture it with a simple "last non-empty line" parse.  Any
    # warnings or progress lines must be written to stderr.
    click.echo(assignment_id)


@main.command(
    "test-chat",
    help=(
        "#314 Phase B: dispatch a test-chat session for a completed work assignment.\n\n"
        "Seeds a `type=\"test-chat\"` `claude -p` worker with the PR diff, "
        "most recent build log, the worker's SMOKE_TESTS block, the repo's "
        "run command, and the repo's CLAUDE.md.  Prints the new assignment id "
        "to stdout.  The TUI captures the id and opens a ChatController overlay "
        "bound to it; developer-typed turns flow via `POST /inject/{id}`.\n\n"
        "Read-plus-Bash — test-chat workers have `Read` and `Bash` tools but "
        "write-side Bash commands (gh, git push, etc.) are blocked by the deny "
        "list in the system prompt.\n\n"
        "WORK_ASSIGNMENT_ID is the id of the work assignment to test (visible "
        "in `coord status` or the TUI Pipeline > Stages tab)."
    ),
)
@click.argument("work_assignment_id")
@click.option(
    "--machine",
    default=None,
    help="Override machine selection (default: first reachable machine that lists the repo).",
)
@_CONFIG_OPTION
def test_chat(work_assignment_id: str, machine: str | None, config_path: Path) -> None:
    """Dispatch a test-chat session for a completed work assignment."""
    from coord.db import get_connection  # noqa: PLC0415

    cfg = _load_config(config_path)

    # Look up the work assignment to resolve the repo name.
    conn = get_connection()
    row = conn.execute(
        "SELECT repo_name FROM assignments WHERE assignment_id=?",
        (work_assignment_id,),
    ).fetchone()
    if row is None:
        click.echo(
            f"error: assignment {work_assignment_id!r} not found in DB",
            err=True,
        )
        sys.exit(1)

    repo = row["repo_name"]
    repo_cfg = cfg.repo(repo)
    if repo_cfg is None:
        click.echo(
            f"error: repo {repo!r} not in coordinator.yml "
            f"(have: {[r.name for r in cfg.repos]})",
            err=True,
        )
        sys.exit(2)

    from coord.test_chat import dispatch_test_chat  # noqa: PLC0415

    try:
        assignment_id, _picked_machine = dispatch_test_chat(
            cfg=cfg,
            repo_cfg=repo_cfg,
            repo=repo,
            work_assignment_id=work_assignment_id,
            machine_override=machine,
        )
    except RuntimeError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    # Print the assignment_id as the LAST line on stdout so callers (the TUI)
    # can capture it with a simple "last non-empty line" parse.
    click.echo(assignment_id)


@main.command(
    "new-issue-chat",
    help=(
        "#316: dispatch a new-issue-chat session for drafting a GitHub issue.\n\n"
        "Seeds a `type=\"new-issue-chat\"` `claude -p` worker with the repo's "
        "CLAUDE.md, the per-repo issue guidance from coordinator.yml, and a "
        "list of recently open issues for near-duplicate detection.  Prints "
        "the new assignment id to stdout — the TUI shells this out and binds "
        "a ChatController overlay to the returned id.\n\n"
        "The worker helps the developer draft a well-structured issue body in "
        "the TITLE: / --- / body format.  It does NOT call `gh issue create`; "
        "submission is handled by the TUI.\n\n"
        "REPO is the local repo name from coordinator.yml."
    ),
)
@click.argument("repo")
@click.option(
    "--machine",
    default=None,
    help="Override machine selection (default: first unpaused machine that lists the repo).",
)
@_CONFIG_OPTION
def new_issue_chat(repo: str, machine: str | None, config_path: Path) -> None:
    cfg = _load_config(config_path)
    repo_cfg = cfg.repo(repo)
    if repo_cfg is None:
        click.echo(
            f"error: repo {repo!r} not in coordinator.yml "
            f"(have: {[r.name for r in cfg.repos]})",
            err=True,
        )
        sys.exit(2)

    from coord.new_issue_chat import dispatch_new_issue_chat

    try:
        assignment_id, _picked_machine = dispatch_new_issue_chat(
            repo,
            cfg,
            machine_override=machine,
        )
    except RuntimeError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    # Print the assignment id as the LAST stdout line so the TUI can capture
    # it with a simple "last non-empty line" parse.
    click.echo(assignment_id)


@main.command(
    "refine-board",
    help=(
        "#316 Phase C: dispatch a board-level refinement chat for a repo.\n\n"
        "Unlike `refine-chat` (which targets a specific issue), this starts an "
        "open-ended `type=\"refinement\"` session for brainstorming new work, "
        "exploring the codebase, or discussing ideas without being tied to any "
        "particular issue.\n\n"
        "Uses ``issue_number=0`` as the sentinel so the TUI routes the chat to "
        "the Board Chat tab rather than a pipeline issue's Refinement tab.  "
        "Prints the new assignment id to stdout — the TUI shells this out and "
        "binds a ChatController overlay to the returned id.\n\n"
        "REPO is the local repo name from coordinator.yml."
    ),
)
@click.argument("repo")
@click.option(
    "--machine",
    default=None,
    help="Override machine selection (default: first unpaused machine that lists the repo).",
)
@_CONFIG_OPTION
def refine_board(repo: str, machine: str | None, config_path: Path) -> None:
    cfg = _load_config(config_path)
    repo_cfg = cfg.repo(repo)
    if repo_cfg is None:
        click.echo(
            f"error: repo {repo!r} not in coordinator.yml "
            f"(have: {[r.name for r in cfg.repos]})",
            err=True,
        )
        sys.exit(2)

    from coord.refine_chat import dispatch_board_refinement

    try:
        assignment_id, _picked_machine = dispatch_board_refinement(
            cfg=cfg,
            repo=repo,
            machine_override=machine,
        )
    except RuntimeError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    # Print the assignment id as the LAST stdout line so the TUI can capture
    # it with a simple "last non-empty line" parse.
    click.echo(assignment_id)


@main.command(
    help=(
        "Mark a refined issue as ready for dispatch.\n\n"
        "Sets the GitHub `status:ready` label and removes `status:refining` / "
        "`status:backlog` if present. After this the issue appears in the "
        "Pipeline panel as Pending with a [Go] button.\n\n"
        "REPO is the local repo name from coordinator.yml; ISSUE is the GH "
        "issue number."
    )
)
@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def ready(repo: str, issue: int, config_path: Path) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    slug = repo_entry.github if repo_entry else repo
    _apply_label_change(
        repo, issue, config_path,
        add={"status:ready"},
        remove_if_present={"status:refining", "status:backlog"},
        success_message=f"#{issue} ({slug}) marked ready for dispatch",
    )


@main.command(
    help=(
        "Mark an issue as in-refinement on GitHub.\n\n"
        "Sets the `status:refining` label and removes `status:ready` if "
        "present so the issue moves out of Refined and back into the "
        "scoping flow.  Symmetric with `coord ready`.\n\n"
        "REPO is the local repo name from coordinator.yml; ISSUE is the "
        "GH issue number."
    )
)
@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def refine(repo: str, issue: int, config_path: Path) -> None:
    """#260: TUI right-click 'Refine' fires this command to move a
    Backlog row into the Refining lifecycle section."""
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    slug = repo_entry.github if repo_entry else repo
    _apply_label_change(
        repo, issue, config_path,
        add={"status:refining"},
        remove_if_present={"status:ready"},
        success_message=f"#{issue} ({slug}) marked status:refining",
    )


@main.command(
    help=(
        "Send an issue to the Pipeline as DISPATCHABLE by tagging it with "
        "both the `coord` and `status:ready` labels on GitHub.\n\n"
        "A dispatchable Pipeline:New card needs BOTH labels.  Coordinator "
        "issues are often *created* with `coord` already, so adding only "
        "`coord` was a no-op that left them stuck without `status:ready` "
        "(#486 Leg 4 bug).  This now ensures both — idempotent: in the normal "
        "Refining → Refined (`coord ready`) → Send flow the issue already has "
        "`status:ready`, so only `coord` is added.  Any pre-Pipeline "
        "`status:refining` / `status:backlog` label is cleared, mirroring "
        "`coord ready`.\n\n"
        "REPO is the local repo name from coordinator.yml; ISSUE is the "
        "GH issue number."
    )
)
@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def track(repo: str, issue: int, config_path: Path) -> None:
    """#261/#486: TUI right-click 'Send to Pipeline' fires this command to
    make the issue a dispatchable Pipeline:New card (`coord` + `status:ready`)."""
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    slug = repo_entry.github if repo_entry else repo
    _apply_label_change(
        repo, issue, config_path,
        add={"coord", "status:ready"},
        remove_if_present={"status:refining", "status:backlog"},
        success_message=(
            f"#{issue} ({slug}) sent to Pipeline (coord + status:ready)"
        ),
        no_op_message=(
            f"#{issue} ({slug}) already dispatchable "
            "(coord + status:ready present)"
        ),
    )


@main.command(
    help=(
        "Drop an issue back to Backlog by removing its `status:*` label.\n\n"
        "Symmetric with `coord refine` / `coord ready` — strips both "
        "`status:refining` and `status:ready` if present, returning the "
        "issue to the unscoped Backlog state.\n\n"
        "REPO is the local repo name from coordinator.yml; ISSUE is the "
        "GH issue number."
    )
)
@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def backlog(repo: str, issue: int, config_path: Path) -> None:
    """#266: TUI right-click 'Drop to Backlog' fires this command to
    walk a Refining/Refined row back to the unscoped Backlog state."""
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    slug = repo_entry.github if repo_entry else repo
    _apply_label_change(
        repo, issue, config_path,
        add=set(),
        remove_if_present={"status:refining", "status:ready"},
        success_message=f"#{issue} ({slug}) dropped to Backlog",
        no_op_message=f"#{issue} ({slug}) already in Backlog (no status:* label)",
    )


@main.command(help="Poll agents and post completion/failure comments on GitHub.")
@_CONFIG_OPTION
def notify(config_path: Path) -> None:
    from coord.hooks import is_round_complete, run_hooks
    from coord.notify import run as run_notify
    from coord.state import build_board, save_board

    cfg = _load_config(config_path)
    posted, stuck = run_notify(cfg)
    if not posted and not stuck:
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
    board = build_board()

    if is_round_complete(board) and cfg.hooks.on_round_complete:
        click.echo("\nRound complete — running hooks:")
        for result in run_hooks("on_round_complete", cfg, board):
            status = "ok" if result.ok else "FAILED"
            click.echo(f"  [{status}] {result.hook}: {result.message}")

    save_board(board)


@main.command(
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
@click.option(
    "--force-merge",
    is_flag=True,
    help="Skip the CI check gate — merge even if checks failed or are still running.",
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
def merge(
    config_path: Path,
    dry_run: bool,
    order: str | None,
    repo_filter: str | None,
    method: str,
    force_merge: bool,
    skip_review: bool,
    skip_smoke: bool,
) -> None:
    from coord import github_ops as gh_ops
    from coord import merge_queue as mq
    from coord.ci_store import build_ci_store
    from coord.merge_queue import CONFLICT, MERGED, PENDING
    from coord.state import load_board

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
    board = load_board()
    open_by_repo, known_by_repo = _load_issue_states()
    # Set of (repo_name, issue_number) for which a `merged` entry already
    # exists.  Avoids spawning a fresh PR for an issue that was already
    # merged via a prior work attempt (multiple work assignments per issue
    # can happen with retries / fix iterations).
    already_merged: set[tuple[str, int]] = set()
    for existing in mq.load_queue():
        if existing.state == MERGED:
            already_merged.add((existing.repo_name, existing.issue_number))

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
    if board is not None:
        for a in board.completed:
            if a.type != "work" or a.status != "done":
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
            # Skip issues whose latest work was already merged (via any
            # prior assignment_id).
            if (a.repo_name, a.issue_number) in already_merged:
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
            entry = mq.enqueue(
                a,
                repo_github=repo_cfg.github,
                target_branch=repo_cfg.default_branch,
            )
            if entry is not None:
                auto_enqueued.append(
                    f"  auto-enqueued: {a.repo_name} #{a.issue_number} "
                    f"({a.branch} → {repo_cfg.default_branch})"
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
            a.type == "work" and a.status == "done" and a.branch
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
        from coord.conflict_fix import dispatch_conflict_fix, has_prior_conflict_fix
        from coord.merge_queue import HUMAN_REQUIRED, classify_conflict
        from coord.state import load_board, save_board

        fix_board = load_board()
        if fix_board is not None:
            dispatched_any = False
            for ev in conflict_events:
                kind = classify_conflict(ev.entry.error)
                if kind == "rebaseable":
                    # Retry cap (#241): if a conflict-fix already ran for this
                    # entry in this session, don't loop — mark HUMAN_REQUIRED
                    # so the user takes over.
                    if has_prior_conflict_fix(fix_board, ev.entry.assignment_id):
                        ev.entry.state = HUMAN_REQUIRED
                        click.echo(
                            f"  {ev.entry.repo_name} #{ev.entry.issue_number}: "
                            "conflict-fix retry cap hit — manual resolution required"
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


def _test_worktree_path(assignment_id: str, repo_name: str) -> Path:
    """#561: throwaway worktree path for `coord test`'s build (per assignment).

    Lives under ``~/.coord/test-worktrees/`` — OUTSIDE the base checkout — so a
    Build never moves the base checkout's branch (which doubles as the live
    editable coordinator source).
    """
    from coord.state import COORD_DIR  # noqa: PLC0415

    return COORD_DIR / "test-worktrees" / f"{repo_name}-{assignment_id}"


def _remove_test_worktree(repo_dir: Path, wt_path: Path) -> None:
    """Best-effort removal of a `coord test` worktree (+ prune admin refs)."""
    import subprocess  # noqa: PLC0415

    if not wt_path.exists():
        return
    for args in (
        ["git", "worktree", "remove", "--force", str(wt_path)],
        ["git", "worktree", "prune"],
    ):
        try:
            subprocess.run(
                args, cwd=str(repo_dir), capture_output=True, text=True, timeout=30
            )
        except (subprocess.SubprocessError, OSError):
            pass


def _cleanup_test_worktree(cfg, assignment) -> None:
    """Remove the test worktree for *assignment* (called on a pass/skip verdict).

    Resolves the base checkout the same way the build path does; a no-op when no
    worktree exists (e.g. a verdict recorded without a prior Build).
    """
    if not assignment.assignment_id:
        return
    repo_dir = _local_repo_dir(cfg, assignment.repo_name)
    if repo_dir is None:
        return
    _remove_test_worktree(
        repo_dir, _test_worktree_path(assignment.assignment_id, assignment.repo_name)
    )


def _local_repo_dir(cfg, repo_name: str) -> Path | None:
    """Resolve the base checkout for *repo_name* (local machine first, then any
    machine that knows it).  Returns an expanded ``Path`` or ``None``."""
    import socket  # noqa: PLC0415

    hostname = socket.gethostname().split(".")[0]
    local_machine = next(
        (m for m in cfg.machines if m.name == hostname or m.host.split(".")[0] == hostname),
        None,
    )
    repo_path = None
    if local_machine:
        repo_path = local_machine.repo_path(repo_name)
    if repo_path is None:
        for m in cfg.machines:
            repo_path = m.repo_path(repo_name)
            if repo_path:
                break
    return Path(repo_path).expanduser() if repo_path else None


def _restore_default_branch_after_test(cfg, assignment) -> None:
    """#271 part 1: switch the local checkout back to the repo's
    `default_branch` after a pass/skip verdict.

    Resolves the repo path the same way `coord test`'s checkout step
    does (local machine's `repo_paths` first, then any machine that
    knows the repo).  Best-effort: a failed `git checkout` is surfaced
    as a warning but doesn't fail the verdict recording.
    """
    import socket  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415

    if not assignment.branch:
        # No branch was ever checked out — nothing to restore.
        return

    repo = cfg.repo(assignment.repo_name)
    if repo is None or not repo.default_branch:
        return

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
        return

    repo_dir = _Path(repo_path).expanduser()
    if not repo_dir.exists():
        return

    # Quick early-out: if the user is already on the default branch
    # (e.g. they switched manually after running `coord test`), there's
    # nothing to do and no need to announce a no-op.
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=5,
        )
        if head.returncode == 0 and head.stdout.strip() == repo.default_branch:
            return
    except (subprocess.TimeoutExpired, OSError):
        # If we can't even check the current branch, don't try to switch.
        return

    try:
        result = subprocess.run(
            ["git", "checkout", repo.default_branch],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        click.echo(f"  warning: could not restore default branch: {e}", err=True)
        return
    if result.returncode != 0:
        # Most common cause: dirty working tree from manual edits during
        # testing.  Surface it so the user can stash + retry manually.
        click.echo(
            f"  warning: could not switch back to {repo.default_branch!r}: "
            f"{result.stderr.strip()}",
            err=True,
        )
        return
    click.echo(f"  restored: {repo.default_branch} in {repo_dir}")


@main.command(help="Pull a worker's branch locally for testing, or record a Test gate verdict.")
@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option("--passed", "verdict", flag_value="pass", help="Mark Test gate as passed.")
@click.option("--fail", "verdict", flag_value="fail", help="Mark Test gate as failed.")
@click.option("--skipped", "verdict", flag_value="skip", help="Mark Test gate as skipped (trivial change).")
@click.option("--reason", default="", help="Reason for failure (used with --fail).")
@click.option("--output", "output_file", type=click.Path(), default=None,
              help="File with test output to store (used with --fail).")
def test(assignment_id: str, config_path: Path, verdict: str | None, reason: str, output_file: str | None) -> None:
    from coord.client import resolve_board_service
    from coord.state import build_board, load_board, record_test_verdict, save_board

    cfg = _load_config(config_path)
    # #590 Phase 2: a thin client reads the board from the daemon (its local DB
    # is empty) so the assignment resolves; the verdict is then recorded back
    # through the daemon. Unset ⇒ unchanged local board + save_board.
    svc = resolve_board_service()
    if svc is not None:
        from coord.client import fetch_remote_board

        board = fetch_remote_board(svc)
    else:
        board = load_board() or build_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    repo = cfg.repo(assignment.repo_name)

    # ── Record verdict ──────────────────────────────────────────────────
    if verdict:
        # Map CLI verdict flags to the canonical test_state values used by the
        # TUI's Test stage and the reconcile review-gating logic.
        test_state_map = {"pass": "passed", "fail": "failed", "skip": "skipped"}
        assignment.test_state = test_state_map[verdict]
        assignment.test_reason = reason if verdict == "fail" else None
        # Mirror to legacy smoke_test for the existing smoke-stage scoring in
        # pipeline.py (which predates the human Test gate).
        if verdict in ("pass", "fail"):
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
                reason_with_output = (
                    f"{reason} [output: {stored}]" if reason else f"[output: {stored}]"
                )
                assignment.test_reason = reason_with_output
                assignment.smoke_test_reason = reason_with_output
                click.echo(f"  test output stored: {stored}")
            else:
                click.echo(f"  warning: output file not found: {output_file}", err=True)

        if svc is not None:
            # Thin client: record the single-row verdict back through the daemon
            # (save_board would write the empty local DB).
            record_test_verdict(
                assignment_id=assignment_id,
                test_state=assignment.test_state,
                test_reason=assignment.test_reason,
                smoke_test=assignment.smoke_test,
                smoke_test_reason=assignment.smoke_test_reason,
            )
        else:
            save_board(board)
        verdict_word = {"pass": "PASSED", "fail": "FAILED", "skip": "SKIPPED"}[verdict]
        click.echo(f"Test gate {verdict_word} for {assignment.repo_name} #{assignment.issue_number}")
        if verdict == "fail" and reason:
            click.echo(f"  reason: {reason}")
        elif verdict == "pass":
            click.echo("  Run: coord merge to proceed")

        # #271 part 1: restore the local checkout to `default_branch` after a
        # pass/skip verdict (legacy safety — #561 means a Build no longer moves
        # the base, so this is a no-op on fresh checkouts), and #561: remove the
        # throwaway test worktree now that testing concluded.  `--fail` leaves
        # the worktree so the user can dig into the failure.
        if verdict in ("pass", "skip"):
            _restore_default_branch_after_test(cfg, assignment)
            _cleanup_test_worktree(cfg, assignment)
        return

    # ── Checkout and build (in a throwaway worktree — #561) ──────────────
    if not assignment.branch:
        click.echo(
            f"error: assignment {assignment_id} has no branch recorded. "
            f"The worker may not have pushed yet, or the branch wasn't captured during reconciliation.",
            err=True,
        )
        sys.exit(1)

    import subprocess

    repo_dir = _local_repo_dir(cfg, assignment.repo_name)
    if repo_dir is None:
        click.echo(
            f"error: no repo_path configured for {assignment.repo_name!r}. "
            f"Add it to coordinator.yml under machines[].repo_paths.",
            err=True,
        )
        sys.exit(1)
    if not repo_dir.exists():
        click.echo(f"error: repo path does not exist: {repo_dir}", err=True)
        sys.exit(1)

    # #561: build/test in a throwaway worktree fetched fresh from origin —
    # NEVER `git checkout` in the base checkout. The base doubles as the live
    # editable coordinator source, so moving its branch silently downgrades the
    # running coord (disabled guards, reintroduced bugs) until restored. A
    # `git fetch` is safe (it doesn't move HEAD); the worktree gets its own.
    wt_path = _test_worktree_path(assignment_id, assignment.repo_name)
    click.echo(
        f"Fetching origin and preparing test worktree for {assignment.branch!r} "
        f"(base checkout {repo_dir} stays untouched)..."
    )
    try:
        subprocess.run(
            ["git", "fetch", "origin", "--prune"], cwd=str(repo_dir),
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        click.echo(f"error: git fetch failed: {e.stderr.strip()}", err=True)
        sys.exit(1)

    # Clear any stale worktree from a prior Build of this assignment.
    _remove_test_worktree(repo_dir, wt_path)
    wt_path.parent.mkdir(parents=True, exist_ok=True)

    def _add_worktree(branch: str):
        # --detach: we only read the tree to build/test; no local branch needed.
        return subprocess.run(
            ["git", "worktree", "add", "--force", "--detach",
             str(wt_path), f"origin/{branch}"],
            cwd=str(repo_dir), capture_output=True, text=True,
        )

    res = _add_worktree(assignment.branch)
    if res.returncode != 0:
        # Branch drift (auto-loop orphan branches; slugifier max_len changes
        # across releases; manual `git branch -m` on origin). When the worktree
        # add fails AND the issue has a PR, resolve the PR's actual headRefName
        # (non-mutating), update the DB, and retry.
        reconciled = _maybe_reconcile_branch(
            assignment, repo_dir, original_error=res.stderr.strip(), config=cfg,
        )
        if reconciled is None:
            click.echo(
                f"error: could not create test worktree: {res.stderr.strip()}",
                err=True,
            )
            sys.exit(1)
        assignment.branch = reconciled
        click.echo(
            f"  branch drift reconciled: using the PR's actual head ref "
            f"{assignment.branch!r}"
        )
        res = _add_worktree(assignment.branch)
        if res.returncode != 0:
            click.echo(
                f"error: could not create test worktree: {res.stderr.strip()}",
                err=True,
            )
            sys.exit(1)

    click.echo(f"Test worktree ready at {wt_path} (branch {assignment.branch!r}).")

    if repo and repo.build_command:
        click.echo(f"Running build: {repo.build_command}")
        result = subprocess.run(repo.build_command, shell=True, cwd=str(wt_path))
        if result.returncode != 0:
            click.echo(f"Build failed (exit {result.returncode})", err=True)
            click.echo(f"  worktree kept for inspection: {wt_path}")
            sys.exit(1)
        click.echo("Build succeeded.")

    if repo and repo.test_command:
        click.echo(f"Running tests: {repo.test_command}")
        result = subprocess.run(repo.test_command, shell=True, cwd=str(wt_path))
        if result.returncode != 0:
            click.echo(f"Tests failed (exit {result.returncode})", err=True)
            click.echo(f"  worktree kept for inspection: {wt_path}")
            sys.exit(1)
        click.echo("Tests passed.")

    click.echo(
        f"\nReady for smoke test (worktree: {wt_path}). Run:\n"
        f"  coord test --passed {assignment_id}   # if it looks good (removes the worktree)\n"
        f"  coord test --fail {assignment_id} --reason \"description\"   # keeps the worktree to dig in"
    )


@main.command(
    "test-plan",
    help=(
        "Generate (or display) a smoke test plan for a completed assignment.\n\n"
        "On first call the plan is generated by calling claude -p (Haiku by default) "
        "with the PR diff, CLAUDE.md, artifact manifest, and issue body.  The result "
        "is cached in the database.  Subsequent calls return the cached plan instantly "
        "without invoking Claude.\n\n"
        "Use --refresh to regenerate and overwrite the cached plan."
    ),
)
@click.argument("assignment_id")
@click.option(
    "--refresh",
    is_flag=True,
    default=False,
    help="Regenerate the plan even if a cached one exists.",
)
@click.option(
    "--model",
    default="haiku",
    show_default=True,
    help="Claude model alias to use for plan generation.",
)
@_CONFIG_OPTION
def test_plan_cmd(
    assignment_id: str,
    refresh: bool,
    model: str,
    config_path: Path,
) -> None:
    """Generate or display the smoke test plan for ASSIGNMENT_ID."""
    from coord.state import get_test_plan, set_test_plan
    from coord.test_orchestrator import find_local_repo_path, generate_plan

    cfg = _load_config(config_path)

    # ── Cache hit path ────────────────────────────────────────────────────
    if not refresh:
        cached = get_test_plan(assignment_id)
        if cached is not None:
            click.echo(json.dumps(cached, indent=2))
            return

    # ── Generate ──────────────────────────────────────────────────────────
    click.echo(
        f"Generating smoke test plan for assignment {assignment_id!r} "
        f"(model: {model})...",
        err=True,
    )
    plan = generate_plan(assignment_id, cfg, model=model)

    # ── Capture branch HEAD SHA for staleness detection ────────────────────
    # Read the assignment's branch from the DB, then resolve the HEAD SHA on
    # the local machine so the TUI can detect when the branch has advanced
    # since this plan was generated and trigger a refresh automatically.
    branch_head = _get_assignment_branch_head(assignment_id, cfg, find_local_repo_path)

    # Persist (always, even the fallback — so a subsequent call without
    # --refresh shows the cached result rather than hitting Claude again).
    # Always write branch_head (even when None) so stale SHAs from a prior
    # run are cleared.
    set_test_plan(assignment_id, plan, branch_head=branch_head)

    click.echo(json.dumps(plan, indent=2))


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

    save_board(board)

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


@main.command(help="Show current session state.")
def session() -> None:
    from coord.state import load_session

    data = load_session()
    if data is None:
        click.echo("No session state found. Start one with coord assign.")
        return

    clean = data.get("clean_shutdown", True)
    started = data.get("started_at", "?")

    if clean:
        ended = data.get("ended_at", "?")
        completed = len(data.get("completed_this_session", []))
        issues = len(data.get("issues_closed", []))
        cost = data.get("total_cost_usd", 0)
        click.echo(f"Last session: {started} → {ended}")
        click.echo(f"  {completed} assignments, {issues} issues, ${cost:.2f}")
    else:
        click.echo(f"Session in progress (started {started})")
        click.echo(f"  clean_shutdown: false (crash recovery may be needed)")
        click.echo(f"  Run: coord resume")


# ── #487: tmux session management ────────────────────────────────────────────


@main.command(
    "sessions",
    help=(
        "List running interactive sessions hosted in tmux (coord-* named sessions). "
        "Use --json for machine-readable output (consumed by coord-tui on startup)."
    ),
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output as JSON (consumed by coord-tui startup check).",
)
@click.option(
    "--remote",
    is_flag=True,
    default=False,
    help=(
        "Also enumerate coord-* sessions on REMOTE fleet machines over ssh+tmux "
        "(#486 Leg 4).  Parallelised; bounded by a 5 s per-host probe."
    ),
)
@_CONFIG_OPTION
def sessions_cmd(output_json: bool, remote: bool, config_path: Path) -> None:
    """List live coord-* tmux sessions with their assignment metadata."""
    import json as _json  # noqa: PLC0415

    from coord.interactive import (  # noqa: PLC0415
        TmuxHost,
        list_coord_tmux_sessions,
        TMUX_SESSION_PREFIX,
    )
    from coord.state import get_connection  # noqa: PLC0415

    # Track which machine each session lives on (None => local) so the TUI /
    # operator knows where a reattach lands.
    session_machine: dict[str, str | None] = {}
    raw: list[dict[str, str]] = []
    for _s in list_coord_tmux_sessions():
        raw.append(_s)
        session_machine.setdefault(_s["session_name"], None)

    # #486 Leg 4: optionally probe REMOTE machines so the TUI can offer reattach
    # to a session launched on another machine.  A local session always wins on
    # name collision.  Down machines fail within the 5 s per-host cap; probes
    # run in parallel so total wall-clock ≈ the slowest single host.
    if remote:
        import concurrent.futures as _cf  # noqa: PLC0415

        try:
            _cfg = _load_config(config_path)
            _local_hn = socket.gethostname().split(".")[0].lower()
            _remotes = [
                m for m in _cfg.machines
                if not (
                    m.name.lower() == _local_hn
                    or m.host.split(".")[0].lower() == _local_hn
                )
            ]
        except Exception:  # noqa: BLE001
            _remotes = []

        def _probe(machine: object) -> tuple[str, list[dict[str, str]]]:
            try:
                # batch=True: this is a background sweep — NEVER prompt for an
                # ssh passphrase (that would hijack the TUI's terminal at
                # startup, #486 Leg 4 regression).  No warm ControlMaster / agent
                # key ⇒ the probe just fails and the machine reports no sessions.
                found = list_coord_tmux_sessions(
                    host=TmuxHost(ssh_target=machine.host, batch=True)  # type: ignore[attr-defined]
                )
                return machine.name, found  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                return machine.name, []  # type: ignore[attr-defined]

        if _remotes:
            with _cf.ThreadPoolExecutor(max_workers=min(8, len(_remotes))) as _ex:
                for _mname, _found in _ex.map(_probe, _remotes):
                    for _s in _found:
                        if _s["session_name"] in session_machine:
                            continue  # local (or earlier) session wins
                        raw.append(_s)
                        session_machine[_s["session_name"]] = _mname

    enriched: list[dict] = []

    # #601: resolve session→assignment metadata (issue_number/repo_name/...) so
    # the TUI can match a live session to its issue row and offer reattach. On a
    # thin client the local DB is retired, so read from the daemon's board when
    # board_service is set; otherwise use the local DB singleton (acquired once —
    # get_connection() is a module-level singleton).
    from coord.client import resolve_board_service  # noqa: PLC0415

    _svc = resolve_board_service()
    _remote_by_aid: dict[str, dict] = {}
    _db_conn = None
    if _svc is not None:
        try:
            from coord.client import fetch_board_payload  # noqa: PLC0415

            _remote_by_aid = {
                a.get("assignment_id"): a
                for a in fetch_board_payload(_svc).get("assignments", [])
            }
        except Exception:  # noqa: BLE001
            _remote_by_aid = {}
    else:
        try:
            _db_conn = get_connection()
        except Exception:  # noqa: BLE001
            _db_conn = None

    for s in raw:
        session_name = s["session_name"]
        assignment_id = session_name[len(TMUX_SESSION_PREFIX):]
        issue_number: int | None = None
        repo_name: str | None = None
        issue_title: str | None = None

        machine_name: str | None = None
        if _svc is not None:
            a = _remote_by_aid.get(assignment_id)
            if a is not None:
                issue_number = a.get("issue_number")
                repo_name = a.get("repo_name")
                issue_title = a.get("issue_title")
                machine_name = a.get("machine_name")
        elif _db_conn is not None:
            try:
                row = _db_conn.execute(
                    "SELECT issue_number, repo_name, issue_title, machine_name "
                    "FROM assignments WHERE assignment_id=?",
                    (assignment_id,),
                ).fetchone()
                if row is not None:
                    issue_number = row["issue_number"] if hasattr(row, "keys") else row[0]
                    repo_name = row["repo_name"] if hasattr(row, "keys") else row[1]
                    issue_title = row["issue_title"] if hasattr(row, "keys") else row[2]
                    machine_name = row["machine_name"] if hasattr(row, "keys") else row[3]
            except Exception:  # noqa: BLE001
                pass

        # Prefer the DB's machine_name (authoritative); fall back to the host
        # the session was discovered on (#486 Leg 4).
        machine = machine_name or session_machine.get(session_name)
        enriched.append(
            {
                "session_name": session_name,
                "assignment_id": assignment_id,
                "issue_number": issue_number,
                "repo_name": repo_name,
                "issue_title": issue_title,
                "machine": machine,
            }
        )

    if output_json:
        click.echo(_json.dumps({"sessions": enriched}))
        return

    if not enriched:
        click.echo("No running interactive sessions.")
        return

    for s in enriched:
        issue_part = f"#{s['issue_number']}" if s["issue_number"] else "(unknown issue)"
        repo_part = s["repo_name"] or "(unknown repo)"
        title_part = f" — {s['issue_title']}" if s["issue_title"] else ""
        machine_part = f" @{s['machine']}" if s.get("machine") else ""
        click.echo(
            f"  {s['session_name']}  {repo_part} {issue_part}{machine_part}{title_part}"
        )
        click.echo(
            f"    reattach: coord reattach {s['assignment_id']}"
            f"  |  tmux attach-session -t {s['session_name']}"
        )


@main.command(
    "reattach",
    help=(
        "Reattach to a running interactive session (tmux) and finalize when done. "
        "The session must have been started with --interactive (tmux required)."
    ),
)
@click.argument("assignment_id")
@_CONFIG_OPTION
def reattach(assignment_id: str, config_path: Path) -> None:
    """Reattach to a live coord-* tmux session.

    When the session ends (operator closes ``claude`` or types ``/exit``),
    the #466 git-floor backstop runs — same as after a normal interactive
    session exit — so the board always gets a terminal state recorded.

    When the session is **already dead** before the user attempts to reattach
    (e.g. the tmux session was killed externally), the backstop also runs to
    release the claim and garbage-collect the orphaned worktree, unblocking a
    subsequent ``coord assign --interactive`` on the same issue.
    """
    import time as _time  # noqa: PLC0415

    from coord.interactive import (  # noqa: PLC0415
        TmuxHost,
        finalize_interactive_exit,
        finalize_remote_interactive_exit,
        tmux_available,
        tmux_session_alive,
        tmux_session_name,
    )

    if not tmux_available():
        click.echo("  error: tmux is not available on this machine.", err=True)
        sys.exit(1)

    sname = tmux_session_name(assignment_id)

    # ── Look up assignment metadata (needed for both live and dead paths) ────
    # Done BEFORE the alive check so the dead-before-attach path can also
    # run finalize_interactive_exit and release the claim.
    repo_name_val: str | None = None
    repo_github_val: str | None = None
    issue_number_val: int | None = None
    machine_name_val: str | None = None
    base_branch_val: str = "main"
    artifact_paths_val: list[str] = []
    # #486 Leg 4: the assignment type + branch decide how a REMOTE session
    # finalizes — a read-only review records DB-only; a fix pushes its remote
    # worktree's commits back to origin.
    assignment_type_val: str | None = None
    branch_val: str | None = None

    # #601: resolve the assignment metadata that finalize_interactive_exit needs
    # (repo/issue/machine/type/branch). On a thin client the local DB is retired,
    # so read from the daemon's board when board_service is set — otherwise the
    # metadata is all null and the session can never be finalized off its blue
    # "running" box. Local DB path is unchanged.
    from coord.client import resolve_board_service  # noqa: PLC0415

    _svc = resolve_board_service()
    if _svc is not None:
        try:
            from coord.client import fetch_board_payload  # noqa: PLC0415

            row = next(
                (
                    a
                    for a in fetch_board_payload(_svc).get("assignments", [])
                    if a.get("assignment_id") == assignment_id
                ),
                None,
            )
        except Exception:  # noqa: BLE001
            row = None
        if row is not None:
            issue_number_val = row.get("issue_number")
            repo_name_val = row.get("repo_name")
            repo_github_val = row.get("repo_github")
            machine_name_val = row.get("machine_name")
            assignment_type_val = row.get("type")
            _br = row.get("branch")
            branch_val = str(_br) if _br else None
    else:
        try:
            from coord.state import get_connection as _gc  # noqa: PLC0415
            conn = _gc()
            row = conn.execute(
                "SELECT issue_number, repo_name, repo_github, machine_name, "
                "type, branch "
                "FROM assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if row is not None:
                def _col(r: object, key: str, idx: int) -> object:  # noqa: ANN001
                    return r[key] if hasattr(r, "keys") else r[idx]  # type: ignore[index]

                issue_number_val = _col(row, "issue_number", 0)  # type: ignore[assignment]
                repo_name_val = str(_col(row, "repo_name", 1))
                repo_github_val = str(_col(row, "repo_github", 2))
                machine_name_val = str(_col(row, "machine_name", 3))
                _at = _col(row, "type", 4)
                assignment_type_val = str(_at) if _at is not None else None
                _br = _col(row, "branch", 5)
                branch_val = str(_br) if _br else None
        except Exception:  # noqa: BLE001
            pass

    # Reconstruct the worktree path and repo_path from coordinator.yml.
    # worktree_path is always ~/.coord/worktrees/<assignment_id> per agent.py.
    from coord.state import COORD_DIR as _COORD_DIR  # noqa: PLC0415
    worktree_path = str(_COORD_DIR / "worktrees" / assignment_id)

    repo_path_val: str | None = None
    # #486 Leg 4: remote-vs-local routing for the attach + finalize.  Defaults
    # to local so an unresolved machine preserves the original local behavior.
    is_local_session: bool = True
    ssh_target_val: str | None = None
    remote_repo_sh: str | None = None
    try:
        cfg = _load_config(config_path)
        # Get default_branch + artifact_paths from the repo config.
        if repo_name_val:
            repo_cfg_obj = next(
                (r for r in cfg.repos if r.name == repo_name_val), None
            )
            if repo_cfg_obj:
                base_branch_val = repo_cfg_obj.default_branch or "main"
                artifact_paths_val = list(repo_cfg_obj.artifact_paths or [])
        # Get repo_path + locality from machine config.
        if machine_name_val and repo_name_val:
            machine_obj = next(
                (m for m in cfg.machines if m.name == machine_name_val), None
            )
            if machine_obj:
                rp = machine_obj.repo_path(repo_name_val)
                if rp:
                    repo_path_val = str(Path(rp).expanduser())
                    # Raw `~/...` → `$HOME/...` so the REMOTE shell (not the
                    # local one) expands it during the push-back finalize.
                    remote_repo_sh = (
                        "$HOME/" + rp[2:]
                        if rp.startswith("~/")
                        else ("$HOME" if rp == "~" else rp)
                    )
                ssh_target_val = machine_obj.host
                _local_hn = socket.gethostname().split(".")[0].lower()
                is_local_session = (
                    machine_obj.name.lower() == _local_hn
                    or machine_obj.host.split(".")[0].lower() == _local_hn
                )
    except Exception:  # noqa: BLE001
        pass

    # The tmux seam: local calls are plain `tmux …`; remote calls become
    # `ssh -t <mux opts> <host> tmux …` (multiplexed via _SSH_MUX_OPTS).
    _tmux_host = (
        TmuxHost(ssh_target=ssh_target_val)
        if (not is_local_session and ssh_target_val)
        else TmuxHost(ssh_target=None)
    )
    _remote_worktree_sh = "$HOME/.coord/worktrees/" + assignment_id

    # ── Shared helper: run finalize backstop and echo results ────────────────
    def _run_finalize(exit_code: int, started_at: float | None = None) -> None:
        if not (repo_name_val and repo_github_val and issue_number_val):
            click.echo(
                "  (assignment metadata not found — skipping git-floor backstop)",
                err=True,
            )
            return
        try:
            # ── REMOTE session (#486 Leg 4) ──────────────────────────────
            # The local git-floor backstop can't see a remote worktree, so a
            # remote FIX pushes its commits back over ssh; everything else
            # records a DB-only terminal state (a review is read-only; remote
            # non-review push-back is deferred — #494/#486d).
            if not is_local_session:
                # A fix/work/plan session wrote commits in a remote worktree on
                # a known branch → push them back (#486d).  (A review is
                # read-only and falls through to the DB-only branch below.)
                #
                # #557 defensive backstop: if branch_val is None (rework/fix
                # assignment was created before the record_dispatched_assignment
                # branch-persist fix landed), try to derive it from the remote
                # worktree's HEAD so we don't strand commits.
                _branch_val = branch_val
                if (
                    assignment_type_val in ("fix", "work", "plan")
                    and not _branch_val
                    and ssh_target_val
                ):
                    try:
                        import subprocess as _sp  # noqa: PLC0415
                        from coord.interactive import (  # noqa: PLC0415
                            _SSH_MUX_OPTS as _MUX,
                        )
                        _probe = _sp.run(
                            [
                                "ssh", *_MUX, ssh_target_val,
                                f"git -C {_remote_worktree_sh}"
                                " rev-parse --abbrev-ref HEAD 2>/dev/null",
                            ],
                            capture_output=True,
                            text=True,
                            timeout=15,
                        )
                        if _probe.returncode == 0:
                            _derived = _probe.stdout.strip()
                            if _derived and _derived != "HEAD":
                                click.echo(
                                    f"  note: branch not in DB — derived from "
                                    f"remote worktree HEAD: {_derived}",
                                    err=True,
                                )
                                _branch_val = _derived
                    except Exception:  # noqa: BLE001
                        pass
                if (
                    assignment_type_val in ("fix", "work", "plan")
                    and _branch_val
                    and remote_repo_sh
                    and ssh_target_val
                ):
                    fr = finalize_remote_interactive_exit(
                        assignment_id=assignment_id,
                        repo_name=repo_name_val,
                        repo_github=repo_github_val,
                        issue_number=int(issue_number_val),  # type: ignore[arg-type]
                        machine_name=machine_name_val or "unknown",
                        ssh_target=ssh_target_val,
                        remote_worktree_sh=_remote_worktree_sh,
                        remote_repo_sh=remote_repo_sh,
                        branch=_branch_val,
                        base_branch=base_branch_val,
                        exit_code=exit_code,
                        started_at=started_at,
                        artifact_paths=artifact_paths_val,
                    )
                    if fr.already_recorded:
                        click.echo(
                            "  result recorded via `coord report-result`; remote "
                            "backstop did not overwrite"
                        )
                    else:
                        click.echo(
                            f"  remote backstop: status={fr.terminal_status} "
                            f"commits_ahead={fr.commits_ahead} pushed={fr.push_ok}"
                        )
                        if not fr.push_ok:
                            click.echo(
                                f"  warning: remote push failed: {fr.push_error}",
                                err=True,
                            )
                            click.echo(
                                f"  fix commits preserved in {_remote_worktree_sh} "
                                f"on {ssh_target_val} (worktree NOT removed)",
                                err=True,
                            )
                    return
                # Read-only review (or a remote write we can't push back):
                # DB-only terminal state so the row doesn't linger as a phantom
                # 'running' worker holding the claim.
                fr2 = finalize_interactive_exit(
                    assignment_id=assignment_id,
                    repo_name=repo_name_val,
                    repo_github=repo_github_val,
                    issue_number=int(issue_number_val),  # type: ignore[arg-type]
                    machine_name=machine_name_val or "unknown",
                    worktree_path=None,
                    base_branch=base_branch_val,
                    exit_code=exit_code,
                    started_at=started_at,
                    log_path=None,
                    repo_path=None,
                )
                if fr2.already_recorded:
                    click.echo(
                        "  result recorded via `coord report-result`; backstop "
                        "did not overwrite"
                    )
                else:
                    click.echo(
                        f"  backstop: status={fr2.terminal_status} (remote, DB-only)"
                    )
                    if assignment_type_val == "review":
                        # #486d: relay the review verdict here — the remote
                        # session can't write this DB — instead of leaving it
                        # a manual `coord report-result` step.
                        _prompt_and_relay_review_verdict(
                            assignment_id=assignment_id,
                            repo_name=repo_name_val,
                            repo_github=repo_github_val,
                            issue_number=int(issue_number_val),  # type: ignore[arg-type]
                            machine_name=machine_name_val or "unknown",
                            verdict_cmd_hint=(
                                f"    coord report-result --assignment "
                                f"{assignment_id} --status done "
                                "--verdict approve|request-changes"
                            ),
                        )
                    elif assignment_type_val is not None:
                        click.echo(
                            "  note: no branch recorded for this remote session "
                            "— any commits remain on its remote worktree; push "
                            "them manually.",
                            err=True,
                        )
                return

            # ── LOCAL session (unchanged) ────────────────────────────────
            finalize_result = finalize_interactive_exit(
                assignment_id=assignment_id,
                repo_name=repo_name_val,
                repo_github=repo_github_val,
                issue_number=int(issue_number_val),  # type: ignore[arg-type]
                machine_name=machine_name_val or "unknown",
                worktree_path=worktree_path,
                base_branch=base_branch_val,
                exit_code=exit_code,
                started_at=started_at,
                log_path=None,
                repo_path=repo_path_val,
                artifact_paths=artifact_paths_val,
            )
            if finalize_result.already_recorded:
                click.echo(
                    "  result already recorded via `coord report-result`; "
                    "backstop did not overwrite",
                )
            else:
                click.echo(
                    f"  backstop: status={finalize_result.terminal_status} "
                    f"commits_ahead={finalize_result.commits_ahead}"
                )
                if not finalize_result.push_ok:
                    click.echo(
                        f"  warning: git push failed: {finalize_result.push_error}",
                        err=True,
                    )
        except Exception as exc:  # noqa: BLE001
            click.echo(
                f"  warning: backstop failed to record completion: {exc}",
                err=True,
            )

    # ── Dead-before-attach: session was killed externally ────────────────────
    # Run finalize here to release the claim and remove the orphaned worktree
    # so the operator can immediately re-dispatch with --interactive.
    if not tmux_session_alive(sname, host=_tmux_host):
        click.echo(f"  session {sname!r} is not alive (it may have ended while you were away).")
        _run_finalize(exit_code=1)
        sys.exit(0)

    # ── Attach ───────────────────────────────────────────────────────────────
    _where = "local" if is_local_session else f"{ssh_target_val} (ssh)"
    click.echo(f"  Attaching to {sname} on {_where} …")
    click.echo("  (detach with Ctrl-b d to leave the session running)")

    started_at = _time.time()
    try:
        import subprocess as _sp  # noqa: PLC0415
        if not is_local_session:
            # Remote: ssh -t into the machine and attach its tmux session
            # (multiplexed via _SSH_MUX_OPTS).  No nesting concern — the remote
            # tmux server is distinct from any local one we're sitting in.
            _reattach_cmd = list(
                _tmux_host.cmd(["attach-session", "-t", sname], tty=True)
            )
        elif os.environ.get("TMUX"):
            # Local + already inside tmux: `attach-session` refuses to nest
            # ("sessions should be nested with care") and exits 1; use
            # `switch-client` to move the current client to the session instead.
            _reattach_cmd = ["tmux", "switch-client", "-t", sname]
        else:
            _reattach_cmd = ["tmux", "attach-session", "-t", sname]
        result = _sp.run(_reattach_cmd)
        exit_code = result.returncode
    except (Exception, KeyboardInterrupt):  # noqa: BLE001
        exit_code = 1

    # After attach returns: check if session ended or user detached.
    if tmux_session_alive(sname, host=_tmux_host):
        click.echo(
            f"\n  Session is still running.  "
            f"Reattach later with: coord reattach {assignment_id}"
        )
        sys.exit(0)

    # ── Session ended — run the finalize backstop ─────────────────────────
    _run_finalize(exit_code=exit_code, started_at=started_at)
    sys.exit(exit_code)


@main.command(help="Show per-assignment and per-model cost breakdown with burn rate.")
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
def usage(config_path: Path, remote: bool, timeout: float) -> None:
    from coord.state import build_board, load_board, load_session
    from coord.usage import build_session_usage, format_usage_report

    board = load_board() or build_board()
    all_assignments = list(board.active) + list(board.completed)

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
    click.echo(format_usage_report(session))


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


@main.command(
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
        f"(db={DB_PATH}, auth={auth})"
    )
    if not token:
        click.echo(
            "  warning: no bearer token — endpoints are open to anyone who can "
            "reach this port. Fine for dev; the production daemon should set one "
            "(echo <secret> > ~/.coord/serve_token). See AGENT_OPERATIONS.md.",
            err=True,
        )
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


def _tail_log(log_path: Path, interval: float = 1.0):
    """Yield new lines from *log_path* as they are written. Like tail -f.

    Stops yielding when the generator is closed by the caller.
    """
    with open(log_path) as f:
        while True:
            line = f.readline()
            if line:
                yield line.rstrip("\n")
            else:
                time.sleep(interval)


def _watch_remote(
    machine,
    assignment_id: str,
    *,
    show_all: bool,
    interval: float,
    timeout: int,
) -> None:
    """Watch a remote assignment by polling the agent's /logs/{id} endpoint.

    Streams log bytes from the remote agent and routes them through the same
    worker_events rendering pipeline used by local watch.  Never returns —
    exits via sys.exit().
    """
    from coord.network import fetch_log
    from coord.worker_events import format_important_event, parse_event, render_event

    deadline = time.monotonic() + timeout
    turn_counter: list[int] = [0]
    since = 0
    is_error = False

    while True:
        if time.monotonic() > deadline:
            click.echo(
                f"error: timed out after {timeout}s waiting for result", err=True
            )
            sys.exit(3)

        try:
            status_code, body = fetch_log(machine, assignment_id, since=since)
        except Exception as e:  # noqa: BLE001
            click.echo(
                f"warning: could not reach agent on {machine.name}: {e}", err=True
            )
            time.sleep(interval)
            continue

        if status_code == 404:
            # Assignment not started yet or log unavailable — keep waiting.
            time.sleep(interval)
            continue

        if status_code != 200:
            click.echo(
                f"error: fetching log from {machine.name} returned HTTP {status_code}",
                err=True,
            )
            sys.exit(1)

        done = False
        if body:
            for raw_line in body.decode("utf-8", errors="replace").splitlines():
                stripped = raw_line.lstrip()
                if not stripped:
                    continue
                if stripped.startswith("#"):
                    if show_all:
                        click.echo(raw_line)
                    continue

                event = parse_event(raw_line)
                if event is None:
                    if show_all:
                        click.echo(raw_line)
                    continue

                if show_all:
                    rendered = render_event(event, turn_counter=turn_counter)
                    if rendered is not None:
                        click.echo(rendered)
                else:
                    important = format_important_event(event)
                    if important is not None:
                        click.echo(important)

                if event.type == "result":
                    is_error = bool(event.raw.get("is_error", False))
                    done = True
                    break

            since += len(body)

        if done:
            break

        time.sleep(interval)

    sys.exit(1 if is_error else 0)


@main.command(help="Watch a running assignment — filtered live log output.")
@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option("--all", "show_all", is_flag=True, help="Show all events, not just important ones.")
@click.option(
    "--interval",
    default=1.0,
    type=float,
    show_default=True,
    help="Poll interval in seconds.",
)
@click.option(
    "--timeout",
    default=1800,
    type=int,
    show_default=True,
    help="Max seconds to wait for the assignment to finish.",
)
def watch(
    assignment_id: str,
    config_path: Path,
    show_all: bool,
    interval: float,
    timeout: int,
) -> None:
    from coord.state import load_dispatched
    from coord.worker_events import format_important_event, parse_event, render_event

    cfg = _load_config(config_path)

    # ── Find the dispatched record ───────────────────────────────────────
    record = next(
        (r for r in load_dispatched() if r.get("assignment_id") == assignment_id),
        None,
    )
    if record is None:
        click.echo(f"error: assignment {assignment_id!r} not found", err=True)
        sys.exit(2)

    # ── Detect whether the assignment lives on a remote agent ────────────
    machine_name = record.get("machine_name", "")
    machine = next((m for m in cfg.machines if m.name == machine_name), None)
    hostname = socket.gethostname().split(".")[0]
    is_remote = machine is not None and (
        machine.name != hostname
        and machine.host.split(".")[0] != hostname
    )

    if is_remote:
        _watch_remote(
            machine,
            assignment_id,
            show_all=show_all,
            interval=interval,
            timeout=timeout,
        )
        return  # _watch_remote exits via sys.exit

    # ── Locate the log file ──────────────────────────────────────────────
    from coord.agent import DEFAULT_STATE_DIR

    log_path = DEFAULT_STATE_DIR / "logs" / f"{assignment_id}.log"

    if not log_path.exists():
        click.echo(f"Waiting for log file: {log_path}")
        deadline_appear = time.monotonic() + 60
        while not log_path.exists() and time.monotonic() < deadline_appear:
            time.sleep(1)
        if not log_path.exists():
            click.echo(
                f"error: log file never appeared: {log_path}", err=True
            )
            sys.exit(2)

    # ── Tail and filter ──────────────────────────────────────────────────
    deadline = time.monotonic() + timeout
    turn_counter = [0]
    is_error = False

    for raw_line in _tail_log(log_path, interval=interval):
        if time.monotonic() > deadline:
            click.echo(
                f"error: timed out after {timeout}s waiting for result", err=True
            )
            sys.exit(3)

        stripped = raw_line.lstrip()
        if not stripped:
            continue
        # Pass through comment/header lines always
        if stripped.startswith("#"):
            if show_all:
                click.echo(raw_line)
            continue

        event = parse_event(raw_line)
        if event is None:
            if show_all:
                click.echo(raw_line)
            continue

        if show_all:
            rendered = render_event(event, turn_counter=turn_counter)
            if rendered is not None:
                click.echo(rendered)
        else:
            important = format_important_event(event)
            if important is not None:
                click.echo(important)

        # Detect terminal result event and exit
        if event.type == "result":
            is_error = bool(event.raw.get("is_error", False))
            break

    sys.exit(1 if is_error else 0)


def _dispatch_followup(
    cfg: Config,
    original: Assignment,
    briefing: str,
    *,
    issue_suffix: str = "",
    model: str | None = None,
    type: str = "work",
    files_likely: list[str] | None = None,
    inherit_branch: bool = True,
) -> str:
    """Dispatch a follow-up assignment for an existing assignment. Returns assignment ID.

    *model* overrides the model tier for the follow-up. When None, the
    dispatcher falls back to ``cfg.models.default``.

    *type* sets the assignment type (``"work"`` or ``"plan"``).  Defaults to
    ``"work"`` so existing callers are unaffected.

    *files_likely* is the list of files the worker is expected to touch.
    When None, an empty list is used (no file constraints).

    *inherit_branch* controls whether the follow-up checks out the parent's
    branch (``target_branch=original.branch``).  True for follow-ups that
    *continue* existing work on the same branch (``coord pr``, smoke-test
    fix-up, continuation).  Must be False when the parent is a read-only
    PLAN assignment: a plan never pushes, its recorded branch is a
    throwaway worktree name (sometimes a stale/wrong capture), and the
    work it spawns must start a FRESH branch derived from the issue.
    """
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
        model=model if model else cfg.models.default,
        type=type,
        files_likely=files_likely if files_likely is not None else [],
        # Pin the follow-up to the parent's branch when one exists AND the
        # caller wants continuation.  Without this, prefixed issue titles
        # like `[fix-1] …` / `[conflict-fix] …` carried into
        # _dispatch_followup (e.g. `coord pr` on a fix-up assignment)
        # cause the agent to slugify the prefixed title and push to an
        # orphan branch instead of the original PR's branch.  But for a
        # plan→work hand-off the parent is read-only and its branch is a
        # throwaway (sometimes wrong) capture, so the work must branch
        # fresh — callers pass inherit_branch=False there.
        target_branch=(original.branch or None) if inherit_branch else None,
    )

    response = dispatch(proposal, cfg)
    assignment_id = response.get("id", "pending")
    record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github=repo.github,
        provider_name=response.get("_provider_name"),
    )

    in_flight = load_dispatched()
    do_not_touch = compute_do_not_touch(proposal, peers=[], in_flight=in_flight)
    post_briefing(proposal, cfg, assignment_id=assignment_id, do_not_touch=do_not_touch)

    # Update board
    board = build_board()
    save_board(board)

    return assignment_id


def _load_plan_for_assignment(assignment, assignment_id: str) -> dict | None:
    """Retrieve the plan dict for a plan-type assignment.

    Tries (in order):
    1. The plan field cached on the assignment object.
    2. The plans table in the DB (populated by `coord notify`).
    3. Parsing the local log file directly (works when agent is local).

    Returns the plan dict or None if not found.
    """
    from coord.state import COORD_DIR, load_plans

    plan_dict = getattr(assignment, "plan", None)
    if plan_dict is None:
        plans = load_plans()
        plan_dict = plans.get(assignment_id)
    if plan_dict is None:
        local_log = COORD_DIR / "logs" / f"{assignment_id}.log"
        try:
            from coord.plan_parser import parse_plan_from_log  # noqa: PLC0415
            worker_plan = parse_plan_from_log(local_log)
        except Exception:  # noqa: BLE001
            worker_plan = None
        if worker_plan is not None:
            plan_dict = worker_plan.to_dict()
    return plan_dict


def _plan_dict_to_text(plan_dict: dict) -> str:
    """Format a WorkerPlan dict into a human-readable text block for briefings."""
    from coord.plan_parser import WorkerPlan  # noqa: PLC0415

    plan = WorkerPlan.from_dict(plan_dict)
    parts: list[str] = []
    if plan.plan:
        parts.append(f"Summary:\n{plan.plan}")
    if plan.files_modify:
        parts.append("Files to modify:\n" + "\n".join(f"  - {f}" for f in plan.files_modify))
    if plan.approach:
        parts.append(f"Approach:\n{plan.approach}")
    if plan.risks:
        parts.append(f"Risks:\n{plan.risks}")
    if plan.estimate:
        parts.append(f"Estimate:\n{plan.estimate}")
    # Smoke tests authored at planning time — the work worker re-emits
    # these (refining if needed) in its own SMOKE_TESTS block before
    # exit.  Surfacing them in the briefing lets the worker copy them
    # verbatim when the change matches the plan.
    if plan.smoke_tests:
        bullets = "\n".join(f"  - {b}" for b in plan.smoke_tests)
        parts.append(f"Smoke tests (from plan — re-emit in your SMOKE_TESTS block):\n{bullets}")
    elif plan.smoke_tests == []:
        parts.append(
            "Smoke tests (from plan): (none — change is internal). "
            "Emit `SMOKE_TESTS: (none — change is internal)` in your block."
        )
    # Fall back to raw_text when no structured sections were found.
    if not parts:
        return plan.raw_text or "(no plan text)"
    return "\n\n".join(parts)


@main.command(help="Dispatch a worker to create a PR for a completed assignment.")
@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option(
    "--no-review",
    is_flag=True,
    default=False,
    help="Skip auto-dispatching an adversarial review after the PR worker.",
)
def pr(assignment_id: str, config_path: Path, no_review: bool) -> None:
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
        f"Use gh pr create. Read the diff (git fetch origin && git diff origin/{default_branch}...HEAD) and write a clear\n"
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

    if not no_review and cfg.reviews.enabled:
        from coord.review import dispatch_review

        fresh_board = load_board() or build_board()
        review = dispatch_review(assignment, fresh_board, cfg)
        if review is not None:
            save_board(fresh_board)
            click.echo(f"Review dispatched (assignment {review.assignment_id})")
            click.echo(f"  reviewer: {review.machine_name}")
        else:
            click.echo("  review not dispatched (no eligible machine or reviews disabled)")


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
        f"Run `git fetch origin && git log --oneline origin/{default_branch}..HEAD` to see what was done.\n"
        f"Run `git diff origin/{default_branch}...HEAD` to see the full diff.\n\n"
        f"## Test failure\n"
        f"{test_output}\n\n"
        f"## Guidance\n"
        f"{guidance_text}\n\n"
        f"## Rules\n"
        f"- Do NOT start over or rewrite from scratch\n"
        f"- Fix the specific test failures\n"
        f"- Commit your fixes and push with git push origin HEAD"
    )

    # Determine escalated model for the fix-up.
    original_model = assignment.model or cfg.models.default
    escalated = cfg.models.next_model(original_model)
    if escalated != original_model:
        click.echo(f"  escalating model: {original_model} → {escalated}")

    try:
        new_id = _dispatch_followup(cfg, assignment, briefing, model=escalated)
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


@main.command(
    "approve-plan",
    help=(
        "Approve a completed plan assignment and dispatch a work assignment "
        "to implement it."
    ),
)
@click.argument("assignment_id")
@_CONFIG_OPTION
def approve_plan(assignment_id: str, config_path: Path) -> None:
    from coord.state import build_board, load_board, save_board

    cfg = _load_config(config_path)
    board = load_board() or build_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    if assignment.type != "plan":
        click.echo(
            f"error: assignment {assignment_id} is type {assignment.type!r}, not 'plan'. "
            "Only plan assignments can be approved with approve-plan.",
            err=True,
        )
        sys.exit(1)

    if assignment.status != "done":
        click.echo(
            f"error: assignment {assignment_id} is {assignment.status!r}, not 'done'. "
            "The plan worker must finish before you can approve it.",
            err=True,
        )
        sys.exit(1)

    plan_dict = _load_plan_for_assignment(assignment, assignment_id)
    if plan_dict is None:
        click.echo(
            f"error: no plan data found for assignment {assignment_id}.\n"
            "Possible reasons: the log is on a remote machine, or the worker "
            "did not output plan sections.\n"
            "Run 'coord notify' after the worker finishes to parse and cache the plan.",
            err=True,
        )
        sys.exit(1)

    plan_text = _plan_dict_to_text(plan_dict)

    # Build the enhanced briefing for the work assignment.
    original_briefing = (assignment.briefing or "").strip()
    separator = "\n\n" if original_briefing else ""
    enhanced_briefing = (
        original_briefing
        + separator
        + "Your plan was reviewed and approved. Implement exactly as described:\n\n"
        + plan_text
    ).strip()

    # Use files_modify from the plan as the allowed-files hint for the worker.
    from coord.plan_parser import WorkerPlan  # noqa: PLC0415
    plan_obj = WorkerPlan.from_dict(plan_dict)
    files_likely = plan_obj.files_modify or assignment.files_allowed or []

    click.echo(
        f"Approving plan {assignment_id}: "
        f"{assignment.repo_name} #{assignment.issue_number} — {assignment.issue_title}"
    )
    click.echo(f"  Dispatching work assignment to {assignment.machine_name}...")

    try:
        new_id = _dispatch_followup(
            cfg,
            assignment,
            enhanced_briefing,
            type="work",
            files_likely=files_likely,
            # The plan is read-only; its recorded branch is a throwaway
            # worktree name (and can be a stale/wrong capture).  Work must
            # branch fresh from the issue, not inherit the plan's branch.
            inherit_branch=False,
        )
    except httpx.HTTPError as e:
        click.echo(f"error: dispatch failed: {e}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    # Persist plan-stage SMOKE_TESTS onto the new work assignment so the
    # TUI surfaces them immediately — and so they survive even if the
    # work worker exits without re-emitting its own block.  The work
    # worker's later SMOKE_TESTS (captured by notify._capture_smoke_tests)
    # overrides this when present.
    if plan_obj.smoke_tests is not None:
        from coord.state import update_assignment_smoke_tests  # noqa: PLC0415
        update_assignment_smoke_tests(new_id, plan_obj.smoke_tests)

    click.echo(f"  Work assignment dispatched (assignment {new_id})")
    click.echo(f"  repo: {assignment.repo_name}  issue: #{assignment.issue_number}")
    click.echo(f"  Run: coord log {new_id} to follow progress")


@main.command(
    "reject-plan",
    help=(
        "Reject a completed plan assignment and re-dispatch for revision "
        "with additional guidance."
    ),
)
@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option(
    "--guidance",
    required=True,
    help="Guidance text explaining what to revise in the plan.",
)
def reject_plan(assignment_id: str, config_path: Path, guidance: str) -> None:
    from coord.state import build_board, load_board, save_board

    cfg = _load_config(config_path)
    board = load_board() or build_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    if assignment.type != "plan":
        click.echo(
            f"error: assignment {assignment_id} is type {assignment.type!r}, not 'plan'. "
            "Only plan assignments can be rejected with reject-plan.",
            err=True,
        )
        sys.exit(1)

    if assignment.status != "done":
        click.echo(
            f"error: assignment {assignment_id} is {assignment.status!r}, not 'done'. "
            "The plan worker must finish before you can reject it.",
            err=True,
        )
        sys.exit(1)

    plan_dict = _load_plan_for_assignment(assignment, assignment_id)
    if plan_dict is None:
        click.echo(
            f"error: no plan data found for assignment {assignment_id}.\n"
            "Possible reasons: the log is on a remote machine, or the worker "
            "did not output plan sections.\n"
            "Run 'coord notify' after the worker finishes to parse and cache the plan.",
            err=True,
        )
        sys.exit(1)

    plan_text = _plan_dict_to_text(plan_dict)

    # Build the enhanced briefing for the revised plan assignment.
    original_briefing = (assignment.briefing or "").strip()
    separator = "\n\n" if original_briefing else ""
    enhanced_briefing = (
        original_briefing
        + separator
        + "Previous plan (rejected):\n\n"
        + plan_text
        + "\n\nGuidance:\n\n"
        + guidance.strip()
    ).strip()

    click.echo(
        f"Rejecting plan {assignment_id}: "
        f"{assignment.repo_name} #{assignment.issue_number} — {assignment.issue_title}"
    )
    click.echo(f"  Re-dispatching revised plan to {assignment.machine_name}...")

    try:
        new_id = _dispatch_followup(
            cfg,
            assignment,
            enhanced_briefing,
            type="plan",
            files_likely=list(assignment.files_allowed),
            # Revised plan is read-only too — don't inherit the prior
            # plan's throwaway branch.
            inherit_branch=False,
        )
    except httpx.HTTPError as e:
        click.echo(f"error: dispatch failed: {e}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    click.echo(f"  Revised plan assignment dispatched (assignment {new_id})")
    click.echo(f"  repo: {assignment.repo_name}  issue: #{assignment.issue_number}")
    click.echo(f"  Run: coord log {new_id} to follow progress")


@main.command(
    "resume-stuck",
    help="Stop a stuck worker and dispatch a continuation with guidance.",
)
@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option("--guidance", required=True, help="Guidance for the continuation worker.")
def resume_stuck(assignment_id: str, config_path: Path, guidance: str) -> None:
    from coord.state import build_board, load_board

    cfg = _load_config(config_path)
    board = load_board() or build_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    if assignment.status != "running":
        click.echo(
            f"error: assignment {assignment_id} is {assignment.status!r}, "
            "can only resume-stuck a running assignment",
            err=True,
        )
        sys.exit(1)

    # Find the machine this assignment is running on
    machine = next(
        (m for m in cfg.machines if m.name == assignment.machine_name), None
    )
    if machine is None:
        click.echo(
            f"error: machine {assignment.machine_name!r} not in config", err=True
        )
        sys.exit(1)

    # Stop the current worker
    try:
        resp = httpx.post(
            f"http://{machine.host}:{AGENT_PORT}/cancel/{assignment_id}",
            timeout=10,
        )
        resp.raise_for_status()
        click.echo(f"Cancelled stuck worker on {machine.name}")
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        click.echo(
            f"warning: could not cancel worker on {machine.name}: {e} "
            "(may have already stopped)",
            err=True,
        )

    # Brief pause for cancellation to take effect
    time.sleep(2)

    # Retrieve the stuck message from the agent's progress data
    stuck_message = ""
    try:
        status_resp = httpx.get(
            f"http://{machine.host}:{AGENT_PORT}/status", timeout=5
        )
        if status_resp.status_code == 200:
            status_data = status_resp.json()
            # Check active and completed for progress info
            for entry in status_data.get("active", []) + status_data.get("completed", []):
                if entry.get("id") == assignment_id:
                    progress = entry.get("progress", {})
                    if progress and progress.get("stuck"):
                        stuck_message = progress["stuck"]
                    break
    except Exception:  # noqa: BLE001
        pass

    repo = cfg.repo(assignment.repo_name)
    if repo is None:
        click.echo(f"error: unknown repo {assignment.repo_name!r}", err=True)
        sys.exit(1)

    default_branch = repo.default_branch

    stuck_section = stuck_message if stuck_message else "(no stuck message captured)"

    briefing = (
        f"You are continuing work on issue #{assignment.issue_number}: {assignment.issue_title}\n\n"
        f"The previous worker got stuck on branch {assignment.branch or 'unknown'}. "
        f"You are already on that branch.\n"
        f"Do NOT start over — continue from where they left off.\n\n"
        f"## What was done\n"
        f"Run `git fetch origin && git log --oneline origin/{default_branch}..HEAD` to see previous work.\n"
        f"Run `git diff origin/{default_branch}...HEAD` to see the full diff.\n\n"
        f"## What the previous worker was stuck on\n"
        f"{stuck_section}\n\n"
        f"## Guidance\n"
        f"{guidance}\n\n"
        f"## Rules\n"
        f"- Continue from the existing branch, do not start over\n"
        f"- Commit your work and push with git push origin HEAD"
    )

    # Determine escalated model for the continuation worker.
    original_model = assignment.model or cfg.models.default
    escalated = cfg.models.next_model(original_model)
    if escalated != original_model:
        click.echo(f"  escalating model: {original_model} → {escalated}")

    try:
        new_id = _dispatch_followup(cfg, assignment, briefing, model=escalated)
    except httpx.HTTPError as e:
        click.echo(f"error: dispatch failed: {e}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    click.echo(f"Continuation dispatched (assignment {new_id})")
    click.echo(f"  branch: {assignment.branch or 'unknown'}")
    click.echo(f"  issue: #{assignment.issue_number}: {assignment.issue_title}")
    click.echo(f"  guidance: {guidance}")


if __name__ == "__main__":
    main()
