"""Shared infra for coord/commands/*.py: config loading, the shared
``--config`` option, port constants, and the handful of helpers used by
more than one command module. Extracted from coord/cli.py (#747)."""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import click

from coord.config import Config, ConfigError, load, resolve_config_path
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
    # Callable default: resolved per-invocation to a concrete Path so every
    # command receives a real path (never None). Resolution order:
    # $COORD_CONFIG → ~/.coord/coordinator.yml → ./coordinator.yml.
    default=resolve_config_path,
    help=(
        "Path to coordinator.yml. Default resolution: $COORD_CONFIG, then "
        "~/.coord/coordinator.yml, then ./coordinator.yml."
    ),
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
        # #803: models config snapshot — TUI reads this to show which model
        # tier will be used for an interactive --fix-of without needing to
        # parse coordinator.yml itself.
        conn.execute(
            "INSERT OR REPLACE INTO board_meta (key, value) VALUES "
            "('pipeline_models', ?)",
            (json.dumps({
                "default": config.models.default,
                "escalation": config.models.escalation,
                "escalate_fix_model": config.pipeline.escalate_fix_model,
            }),),
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
        # #1151: repo_name -> route `match` globs, for repos whose acceptance
        # driver is *routed* (acceptance.drivers.<repo>.routes non-empty,
        # #1125). Unrouted repos (flat driver or no driver at all) are
        # omitted entirely. The TUI's Pipeline right-click acceptance actions
        # (`dispatch_gate_a_mock_for_selected_pipeline_row` /
        # `dispatch_acceptance_author_for_selected_pipeline_row` /
        # `dispatch_acceptance_record_for_selected_pipeline_row`, all in
        # tui/src/app/pipeline.rs) were firing `coord acceptance mock/author/
        # record` with no `--for-path`, which those CLI commands reject with
        # "no route matched" the moment a repo's driver becomes routed. This
        # lets the TUI auto-resolve the unambiguous (single-route) case and
        # surface a clear, actionable warning — instead of a raw CLI error —
        # when more than one route exists and it can't tell which applies.
        acceptance_routes_map: dict[str, list[str]] = {
            repo_name: [route.match for route in driver.routes]
            for repo_name, driver in config.acceptance.drivers.items()
            if driver.routes
        }
        conn.execute(
            "INSERT OR REPLACE INTO board_meta (key, value) VALUES "
            "('pipeline_acceptance_routes', ?)",
            (json.dumps(acceptance_routes_map),),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 — non-critical, don't abort CLI
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass


def _load_config(path: Path | None) -> Config:
    # Resolve the default location ($COORD_CONFIG → ~/.coord/coordinator.yml →
    # ./coordinator.yml) when no explicit --config was given, so `coord` works on
    # a machine without a repo checkout and isn't sensitive to the CWD.
    if path is None:
        from coord.config import resolve_config_path  # noqa: PLC0415

        path = resolve_config_path()
    # #1080: "am I a thin client" (board_service configured) is the PRIMARY
    # branch, checked before "does a local file exist" — not after. A thin
    # client must never trust a local coordinator.yml, even one that happens
    # to exist: a stray ~/.coord/coordinator.yml or ./coordinator.yml can
    # silently diverge from the daemon's real config with no signal that it's
    # stale (#947 friction log — a 7-week-old symlink shadowed the daemon's
    # config on every command). On a machine with no client.toml/board_service
    # (svc is None — e.g. the daemon host), this is a no-op and local-file
    # resolution proceeds exactly as before (#584/#591).
    try:
        from coord.client import resolve_board_service  # noqa: PLC0415

        svc = resolve_board_service()
        if svc is not None:
            from coord.client import fetch_remote_config  # noqa: PLC0415

            try:
                path = fetch_remote_config(svc)
            except Exception as exc:  # noqa: BLE001 — do NOT fall through to
                # load(path): path may point at a local file that happens to
                # exist (the exact bypass this issue closes). Fail loudly
                # instead of silently trusting whatever is on disk.
                raise ConfigError(
                    f"could not fetch config from {svc.url}: {exc}"
                ) from exc
        cfg = load(path)
    except ConfigError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(2)
    _save_config_snapshot(cfg)
    return cfg


def _not_implemented(name: str) -> None:
    click.echo(f"coord {name}: not implemented yet (stub)", err=True)
    sys.exit(1)


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
    """Shared backbone for the lifecycle label-change commands
    (#260/#261/#266/#802).

    Resolves *repo* via ``coordinator.yml``, then delegates to
    ``state.apply_issue_labels`` which routes through the daemon seam
    (GitHub via ``gh`` today; GitLab / bare-DB later) — the same seam
    ``coord issue label`` uses. The local ``issues`` cache is updated
    inside the seam so the TUI reflects the change on its next tick.

    ``no_op_message`` (optional) is echoed when no labels were actually
    added or removed — used by ``coord backlog`` to say "already in
    Backlog" instead of making a no-op ``gh`` call.
    """
    from coord.state import apply_issue_labels  # noqa: PLC0415

    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r} (not in coordinator.yml)", err=True)
        sys.exit(1)
    slug = repo_entry.github

    try:
        _new_labels, changed = apply_issue_labels(
            repo, issue,
            add=add,
            remove=remove_if_present,
            repo_github=slug,
        )
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: label change failed: {e}", err=True)
        sys.exit(1)

    if not changed and no_op_message is not None:
        click.echo(no_op_message)
        return

    click.echo(success_message)