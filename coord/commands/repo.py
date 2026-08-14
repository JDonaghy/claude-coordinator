"""``coord repo add`` / ``coord repo doctor`` — onboarding a repo, and
checking that it actually happened (#2220).

Onboarding a repo is ~14 steps across five layers (config, three machines,
GitHub, the repo's own contents, the graph) with no command, no checklist and
no verifier — reconstructed from memory each time, and **every layer fails
silently**. ``stick-demo`` sat two-thirds onboarded until ``stick-demo#1`` died
of it, having burned both drive attempts, while ``coord config``, ``coord
status`` and ``coord assign --dry-run`` all reported the dispatch as fine.

Of the two commands here the **second is the one that matters**. A runbook is
the weakest available answer and this codebase already has the proof:
``docs/GRAPHIFY_SETUP.md`` is exactly that shape, and graphify has still fallen
by the wayside more than once because nothing checks it. What holds is a
checkable gate — ``coord diagnose --graph``, ``coord doctor``, ``coord release
verify`` all work because they answer *is it true right now*, not *did you
remember*.

``coord repo add`` therefore does only the mechanical, safely-automatable parts
and **prints the residue it deliberately did not do**, rather than pretending
completeness. The parts it skips (clone, agent restart, CLAUDE.md, CI workflow)
are the ones a wrong guess makes worse, and they are exactly what ``coord repo
doctor`` then verifies.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from coord.commands._common import _CONFIG_OPTION, _load_config

# Reuse the fleet's own definition of where the tracked config lives (#1779) —
# `~/src/coord-settings/coord/coordinator.yml`, NOT the `~/.coord/` symlink
# (#1832): edits must land in the checkout so they can be committed, reviewed
# and pulled onto the daemon host.
from coord.fleet_config_health import TRACKED_CONFIG_REL, default_settings_dir


@click.group("repo", help="Add a repo to the fleet, and verify it is actually onboarded.")
def repo_group() -> None:
    """Repo onboarding (#2220)."""


def _resolve_write_target(explicit: Path | None) -> Path:
    """Where ``coord repo add`` writes.

    Defaults to the coord-settings checkout's tracked file rather than
    whatever ``resolve_config_path()`` returns, because the live
    ``~/.coord/coordinator.yml`` is a *symlink* into that checkout: writing
    through the symlink produces an untracked, uncommitted change to the
    fleet's governing config with nothing to review and nothing to pull
    (#1779/#1832). Refuses rather than falling back when the checkout is
    absent — a machine with no coord-settings checkout is deliberately not
    allowed to edit the fleet's config.
    """
    if explicit is not None:
        return explicit
    tracked = default_settings_dir() / TRACKED_CONFIG_REL
    if not tracked.exists():
        raise click.ClickException(
            f"no coord-settings checkout at {default_settings_dir()} (expected "
            f"{tracked}). `coord repo add` writes the TRACKED config so the "
            "change can be committed, reviewed and pulled — it will not write "
            "through the ~/.coord symlink (#1832). Clone coord-settings, set "
            "$COORD_SETTINGS_DIR, or pass --config explicitly."
        )
    return tracked


@repo_group.command(
    "add",
    help=(
        "Write a new repo's coordinator.yml entry into the coord-settings "
        "checkout, add it to the named machines, create the `coord` and tier "
        "labels — then print the residue it deliberately did NOT do."
    ),
)
@click.argument("name")
@click.option("--github", "github_slug", required=True, help="owner/repo on GitHub.")
@click.option(
    "--machines", "machines_csv", default=None,
    help="Comma-separated machine names that should serve this repo.",
)
@click.option(
    "--repo-path", "repo_path_tmpl", default=None,
    help=(
        "Path to the clone on each machine. Default: ~/src/<name> — the fleet "
        "convention, and the worker WORKTREE BASE, not a convenience checkout."
    ),
)
@click.option(
    "--default-branch", "default_branch_override", default=None,
    help=(
        "Override the default branch instead of reading the real one from "
        "GitHub. Use only when GitHub is unreachable — a default_branch that "
        "disagrees with the repo's real default silently routes worker PRs to "
        "the wrong base."
    ),
)
@click.option("--build-command", default=None, help="repos[].build_command.")
@click.option("--test-command", default=None, help="repos[].test_command.")
@click.option(
    "--labels/--no-labels", "do_labels", default=True, show_default=True,
    help="Create the `coord` and tier:small/tier:large labels on GitHub.",
)
@click.option(
    "--dry-run", is_flag=True, default=False,
    help="Print the edited config and the residue without writing anything.",
)
@click.option(
    "--config", "config_path", type=click.Path(path_type=Path), default=None,
    help="coordinator.yml to edit. Default: the coord-settings tracked file.",
)
def repo_add(  # noqa: PLR0913 — one option per thing the command can set
    name: str,
    github_slug: str,
    machines_csv: str | None,
    repo_path_tmpl: str | None,
    default_branch_override: str | None,
    build_command: str | None,
    test_command: str | None,
    do_labels: bool,  # noqa: FBT001
    dry_run: bool,  # noqa: FBT001
    config_path: Path | None,
) -> None:
    from coord.config import load as load_config  # noqa: PLC0415
    from coord.repo_edit import (  # noqa: PLC0415
        RepoEditError,
        add_repo_to_machine,
        insert_repo_entry,
        render_repo_entry,
    )
    from coord.repo_onboard import COORD_LABEL, TIER_LABELS  # noqa: PLC0415

    target = _resolve_write_target(config_path)
    original = target.read_text(encoding="utf-8")

    try:
        cfg = load_config(target)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(f"{target} does not currently load: {exc}") from exc

    if cfg.repo(name) is not None:
        raise click.ClickException(
            f"repo {name!r} already has a repos[] entry in {target} — use "
            f"`coord repo doctor {name}` to find what is actually missing."
        )

    known = {m.name for m in cfg.machines}
    machines = [m.strip() for m in (machines_csv or "").split(",") if m.strip()]
    unknown = [m for m in machines if m not in known]
    if unknown:
        raise click.ClickException(
            f"unknown machine(s) {unknown} — coordinator.yml has {sorted(known)}"
        )

    # ── The real default branch, read from GitHub rather than trusted ────
    if default_branch_override:
        default_branch = default_branch_override
        branch_source = "--default-branch (NOT verified against GitHub)"
    else:
        from coord import github_ops  # noqa: PLC0415

        try:
            default_branch = github_ops.get_repo_default_branch(github_slug)
        except Exception as exc:  # noqa: BLE001
            raise click.ClickException(
                f"could not read {github_slug}'s default branch from GitHub: "
                f"{exc}. Fix `gh` auth, or pass --default-branch explicitly "
                "(and know that an unverified value silently routes worker PRs "
                "to the wrong base)."
            ) from exc
        branch_source = f"read from GitHub ({github_slug})"

    entry = render_repo_entry(
        name, github_slug, default_branch,
        build_command=build_command, test_command=test_command,
    )
    try:
        updated = insert_repo_entry(original, entry)
        path_tmpl = repo_path_tmpl or f"~/src/{name}"
        for machine in machines:
            updated = add_repo_to_machine(updated, machine, name, path_tmpl)
    except RepoEditError as exc:
        raise click.ClickException(str(exc)) from exc

    # ── Seatbelt: the edit must produce a config that LOADS and contains
    # what we think it contains. A line-level edit that "worked" but left a
    # repo unroutable is worse than no command at all (#2220's whole thesis).
    import tempfile  # noqa: PLC0415

    with tempfile.NamedTemporaryFile(
        "w", suffix=".yml", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(updated)
        probe_path = Path(fh.name)
    try:
        new_cfg = load_config(probe_path)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(
            f"refusing to write: the edited config does not parse ({exc}). "
            f"{target} is unchanged."
        ) from exc
    finally:
        probe_path.unlink(missing_ok=True)

    if new_cfg.repo(name) is None:
        raise click.ClickException(
            f"refusing to write: the edit parsed but repo {name!r} is not in "
            f"the result. {target} is unchanged."
        )
    landed = [m.name for m in new_cfg.machines if name in (m.repos or [])]
    missing = [m for m in machines if m not in landed]
    if missing:
        raise click.ClickException(
            f"refusing to write: the edit parsed but machine(s) {missing} do "
            f"not list {name!r}. {target} is unchanged."
        )

    if dry_run:
        click.echo(f"--dry-run: would write {target}")
        click.echo(updated)
    else:
        target.write_text(updated, encoding="utf-8")
        click.echo(f"✓ wrote repos[{name}] to {target}")
        click.echo(f"  default_branch: {default_branch}  ({branch_source})")
        if landed:
            click.echo(f"  machines: {', '.join(landed)}")

    # ── Labels ───────────────────────────────────────────────────────────
    created: list[str] = []
    label_failures: list[str] = []
    if do_labels and not dry_run:
        from coord import github_ops  # noqa: PLC0415

        for label, colour, desc in (
            (COORD_LABEL, "0e8a16", "Managed by the coord Pipeline"),
            (TIER_LABELS[0], "c2e0c6", "Route to the small/cheap model"),
            (TIER_LABELS[1], "5319e7", "Route to the large model"),
        ):
            try:
                github_ops.create_label(
                    github_slug, label, color=colour, description=desc
                )
                created.append(label)
            except Exception as exc:  # noqa: BLE001
                label_failures.append(f"{label}: {exc}")
        if created:
            click.echo(f"✓ labels ensured on {github_slug}: {', '.join(created)}")
        for failure in label_failures:
            click.echo(f"⚠ label creation failed — {failure}", err=True)

    # ── Residue: what this command deliberately did NOT do ───────────────
    click.echo("")
    click.echo("NOT DONE — these need a human, and `coord repo doctor` checks each:")
    tracked = default_settings_dir() / TRACKED_CONFIG_REL
    if target == tracked:
        click.echo(
            f"  1. commit + push in {default_settings_dir()}, then `git pull` on "
            "the daemon host — the fleet runs the COMMITTED config"
        )
    else:
        click.echo(
            f"  1. commit + push {target} wherever it is tracked, then `git "
            "pull` on the daemon host — the fleet runs the COMMITTED config"
        )
    for machine in machines or ["<each machine>"]:
        click.echo(
            f"  2. clone the repo to {repo_path_tmpl or f'~/src/{name}'} on "
            f"{machine} — this is the worker WORKTREE BASE"
        )
    click.echo(
        "  3. RESTART coord-agent on each of those machines. The repo list is "
        "frozen at process start (#2219), so a running agent will refuse every "
        "dispatch for this repo while config says it is supported. This cannot "
        "run while headless workers are live."
    )
    click.echo(
        "  4. add a CLAUDE.md to the repo — the Test agent auto-loads it and "
        "the adversarial review prompt is assembled from it; without one, "
        "reviews enforce nothing."
    )
    click.echo(
        "  5. make sure at least one CI workflow triggers on `pull_request`. "
        "If none does, expects_checks() reads 'CI exists' while zero checks "
        "arrive and `checks_absent` blocks EVERY merge in this repo, forever."
    )
    click.echo(
        "  6. set `test_command`/`ci_command`, `smoke_tests.capability_rules` "
        "for this repo's paths, and (if it joins the oracle loop) "
        "`acceptance.drivers`."
    )
    click.echo(
        "  7. graphify build + `graphify hook install` + `core.hooksPath`, in "
        "that order, per machine (docs/GRAPHIFY_SETUP.md)."
    )
    click.echo("")
    click.echo(f"Then: coord repo doctor {name}")


@repo_group.command(
    "doctor",
    help=(
        "Probe all five onboarding layers for a repo and report per-layer "
        "status. Reads LIVE state — each agent's /health repo list, the labels "
        "that exist on GitHub, whether any workflow triggers on pull_request — "
        "not config. Exits non-zero on any CRIT so it can gate."
    ),
)
@click.argument("name")
@_CONFIG_OPTION
@click.option(
    "--timeout", default=3.0, show_default=True, type=float,
    help="Per-machine /health timeout (seconds).",
)
@click.option(
    "--github/--no-github", "probe_github", default=True, show_default=True,
    help="Probe GitHub (labels, workflows, CLAUDE.md). Off makes this offline.",
)
@click.option(
    "--verbose", "-v", is_flag=True, default=False,
    help="Show passing checks too, not just the residue.",
)
def repo_doctor(
    name: str,
    config_path: Path,
    timeout: float,
    probe_github: bool,  # noqa: FBT001
    verbose: bool,  # noqa: FBT001
) -> None:
    from coord import repo_onboard  # noqa: PLC0415
    from coord.network import check_all  # noqa: PLC0415

    cfg = _load_config(config_path)

    repo = cfg.repo(name)
    if repo is None:
        known = [r.name for r in cfg.repos]
        click.echo(
            f"error: repo {name!r} is not in coordinator.yml (have: {known})",
            err=True,
        )
        # Still render the report — "config.repo_missing" IS the finding, and
        # a caller gating on this deserves the same structured output.
        facts = repo_onboard.RepoFacts(name=name, configured=False)
        for line in repo_onboard.format_report(
            repo_onboard.evaluate(facts), verbose=verbose
        ):
            click.echo(line)
        sys.exit(1)

    machines = [m for m in cfg.machines if name in (m.repos or [])]
    statuses = check_all(machines, timeout=timeout) if machines else []

    facts = repo_onboard.gather_facts(
        cfg, name,
        statuses=statuses,
        probe_github=probe_github,
        local_clone=repo_onboard.local_clone_path(cfg, name),
    )
    report = repo_onboard.evaluate(facts)
    for line in repo_onboard.format_report(report, verbose=verbose):
        click.echo(line)
    if not report.ok:
        sys.exit(1)
