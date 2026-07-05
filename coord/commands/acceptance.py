"""``coord acceptance`` — the framework-agnostic oracle-loop runner (#944,
docs/ORACLE_LOOP.md).

Two subcommands:

- ``coord acceptance run --repo R (--issue N | --all)`` — run the repo's
  declared driver **in-session** (the worker's own warm loop) and print a
  structured pass/fail verdict. Sealed: verdicts only, never test source.
- ``coord acceptance record --repo R --issue N --sha SHA`` — the
  coordinator's **external** trust gate: re-run the sealed slice against the
  pushed SHA in a throwaway worktree and write the verdict to the board (the
  Acceptance box). Routes the whole command through the daemon (mirrors
  ``coord merge`` / ``coord diagnose`` — the no-local-DB rule), never a bare
  ``save_board``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import click

from coord.acceptance import (
    ACCEPTANCE_DIRNAME,
    build_verdict,
    dump_manifest_error_hint,
    failure_summary,
    load_manifest,
    test_ids_for_issue,
)
from coord.acceptance_drivers import DriverError, run_driver
from coord.commands._common import _CONFIG_OPTION, _load_config


@click.group("acceptance")
def acceptance_group() -> None:
    """The oracle-loop acceptance runner.

    A thin, framework-agnostic front end over a per-repo driver adapter
    declared in ``coordinator.yml`` (``acceptance.drivers``). ``run`` is what
    a worker calls in its own warm session to check itself against the
    sealed suite; ``record`` is the coordinator's external re-run against a
    pushed SHA — the trust gate a headless worker can't fake.
    """


def _resolve_driver(cfg, repo: str):
    driver_cfg = cfg.acceptance.driver_for(repo)
    if driver_cfg is None:
        click.echo(
            f"error: no acceptance driver configured for repo {repo!r} "
            "(add it under acceptance.drivers in coordinator.yml)",
            err=True,
        )
        sys.exit(1)
    return driver_cfg


def _scoped_verdict(tests: list[dict], acceptance_root: Path, issue_number: int) -> dict:
    """Filter *tests* down to *issue_number*'s manifest slice, or exit(1) with
    a clear message when the manifest / slice doesn't exist yet."""
    manifest = load_manifest(acceptance_root)
    if not manifest:
        click.echo(f"error: {dump_manifest_error_hint(acceptance_root)}", err=True)
        sys.exit(1)
    ids = test_ids_for_issue(manifest, issue_number)
    if not ids:
        click.echo(
            f"error: issue #{issue_number} has no acceptance slice in the "
            "manifest yet.",
            err=True,
        )
        sys.exit(1)
    scoped = [t for t in tests if t["id"] in ids]
    return build_verdict(scoped, scope="issue", issue_number=issue_number)


@acceptance_group.command("run")
@click.option("--repo", required=True, help="Local repo name (coordinator.yml repos[].name).")
@click.option(
    "--issue", "issue_number", type=int, default=None,
    help="Issue number to scope the verdict to (mutually exclusive with --all).",
)
@click.option(
    "--all", "run_all", is_flag=True,
    help="Run + report the full accumulated suite (Gate C) instead of one issue's slice.",
)
@click.option(
    "--path", "path_opt", type=click.Path(file_okay=False), default=None,
    help="Repo checkout to run the driver in (default: current directory).",
)
@_CONFIG_OPTION
def acceptance_run(
    repo: str,
    issue_number: int | None,
    run_all: bool,
    path_opt: str | None,
    config_path: Path,
) -> None:
    """Run REPO's sealed acceptance suite and print a structured verdict.

    The in-session command a worker runs to check itself: ``coord acceptance
    run --issue N`` iterates against the sealed oracle without needing to see
    inside it — only pass/fail + failure messages are ever printed, never
    test source.
    """
    if not run_all and issue_number is None:
        click.echo("error: pass --issue N or --all", err=True)
        sys.exit(1)
    if run_all and issue_number is not None:
        click.echo("error: --issue and --all are mutually exclusive", err=True)
        sys.exit(1)

    cfg = _load_config(config_path)
    driver_cfg = _resolve_driver(cfg, repo)
    cwd = Path(path_opt).expanduser() if path_opt else Path.cwd()

    try:
        result = run_driver(driver_cfg.kind, driver_cfg.run, cwd=str(cwd))
    except DriverError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    if not result.tests and result.exit_code != 0:
        # Compile error / crash before any test emitted a verdict — surface
        # the driver's raw output so the worker can actually act on it,
        # rather than a bare "0 tests found".
        click.echo(result.raw_output, err=True)

    if run_all:
        verdict = build_verdict(result.tests, scope="all")
    else:
        verdict = _scoped_verdict(result.tests, cwd / ACCEPTANCE_DIRNAME, issue_number)

    click.echo(json.dumps(verdict, indent=2))
    if verdict["total"] == 0 or not verdict["green"]:
        sys.exit(1)


def _acceptance_worktree_path(repo_name: str, issue_number: int) -> Path:
    """Throwaway worktree path for ``coord acceptance record``'s external
    re-run.  Lives under ``~/.coord/acceptance-worktrees/`` — OUTSIDE the base
    checkout, same rationale as ``coord test``'s ``_test_worktree_path``
    (#561): a Build/record must never move the base checkout's branch (it
    doubles as the live editable coordinator source on some machines)."""
    from coord.state import COORD_DIR

    return COORD_DIR / "acceptance-worktrees" / f"{repo_name}-{issue_number}"


def _remove_acceptance_worktree(repo_dir: Path, wt_path: Path) -> None:
    if not wt_path.exists():
        return
    for args in (
        ["git", "worktree", "remove", "--force", str(wt_path)],
        ["git", "worktree", "prune"],
    ):
        try:
            subprocess.run(
                args, cwd=str(repo_dir), capture_output=True, text=True, timeout=30,
            )
        except (subprocess.SubprocessError, OSError):
            pass


def _acceptance_record_via_daemon(svc, params: dict) -> None:
    """Run ``coord acceptance record`` on the daemon host (where the
    canonical board + the repo checkouts live) and relay its output.
    Mirrors ``_diagnose_via_daemon`` / ``_reconcile_via_daemon``."""
    from coord.client import post_record  # noqa: PLC0415

    try:
        resp = post_record(svc, "/acceptance-record", params, timeout=900.0)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: acceptance record via daemon failed: {exc}", err=True)
        sys.exit(1)
    output = resp.get("output") or ""
    if output:
        click.echo(output, nl=False)
    if resp.get("error"):
        click.echo(f"error: {resp['error']}", err=True)
    code = resp.get("exit_code") or 0
    if code:
        sys.exit(int(code))


@acceptance_group.command("record")
@click.option("--repo", required=True, help="Local repo name (coordinator.yml repos[].name).")
@click.option("--issue", "issue_number", type=int, required=True, help="Issue number.")
@click.option(
    "--sha", "sha", required=True,
    help="Commit SHA to check out and re-run the sealed suite against — the trust gate.",
)
@_CONFIG_OPTION
def acceptance_record(
    repo: str,
    issue_number: int,
    sha: str,
    config_path: Path,
) -> None:
    """Re-run REPO's issue-N acceptance slice externally against SHA and
    write the verdict to the board (the Acceptance box).

    A headless worker can lie about "green" in its own session; it can't
    fake the coordinator re-running the sealed suite itself, against the
    exact SHA it pushed, in a throwaway worktree the worker never touches.
    """
    from coord.board_service import daemon_reroute_target  # noqa: PLC0415

    # #944: the canonical board + the repo checkouts live on the daemon host,
    # so a thin client routes the ENTIRE record run there (mirrors `coord
    # merge` / `coord diagnose` — never a bare save_board from a thin
    # client's empty local DB). COORD_ACCEPTANCE_ON_DAEMON guards the daemon
    # against re-routing to itself (set by the /acceptance-record server
    # route before it calls this callback directly).
    svc = daemon_reroute_target("COORD_ACCEPTANCE_ON_DAEMON")
    if svc is not None:
        _acceptance_record_via_daemon(
            svc, {"repo": repo, "issue": issue_number, "sha": sha},
        )
        return

    _acceptance_record_local(repo, issue_number, sha, config_path)


def _acceptance_record_local(
    repo: str, issue_number: int, sha: str, config_path: Path,
) -> None:
    from coord.test_orchestrator import find_local_repo_path  # noqa: PLC0415

    cfg = _load_config(config_path)
    driver_cfg = _resolve_driver(cfg, repo)

    repo_dir = find_local_repo_path(repo, cfg)
    if repo_dir is None or not repo_dir.exists():
        click.echo(
            f"error: no local repo checkout found for {repo!r} "
            "(repo_paths in coordinator.yml)",
            err=True,
        )
        sys.exit(1)

    wt_path = _acceptance_worktree_path(repo, issue_number)
    click.echo(
        f"Fetching origin and preparing acceptance worktree at {sha!r} "
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

    _remove_acceptance_worktree(repo_dir, wt_path)
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        ["git", "worktree", "add", "--force", "--detach", str(wt_path), sha],
        cwd=str(repo_dir), capture_output=True, text=True,
    )
    if res.returncode != 0:
        click.echo(
            f"error: could not create acceptance worktree at {sha!r}: "
            f"{res.stderr.strip()}",
            err=True,
        )
        sys.exit(1)

    try:
        result = run_driver(driver_cfg.kind, driver_cfg.run, cwd=str(wt_path))
    except DriverError as e:
        click.echo(f"error: {e}", err=True)
        _remove_acceptance_worktree(repo_dir, wt_path)
        sys.exit(1)

    verdict = _scoped_verdict(result.tests, wt_path / ACCEPTANCE_DIRNAME, issue_number)

    from coord.board_service import read_board  # noqa: PLC0415
    from coord.diagnose import stage_assignments  # noqa: PLC0415

    board = read_board()
    work_rows = stage_assignments(board, repo, issue_number, "work")
    if not work_rows:
        click.echo(
            f"error: no work assignment found for {repo} #{issue_number}; "
            "cannot record verdict",
            err=True,
        )
        sys.exit(1)
    assignment_id = work_rows[0].assignment_id

    from coord.state import record_acceptance_verdict  # noqa: PLC0415

    acceptance_state = "passed" if verdict["green"] else "failed"
    reason = failure_summary(verdict) or None
    record_acceptance_verdict(
        assignment_id=assignment_id,
        acceptance_state=acceptance_state,
        acceptance_reason=reason,
        acceptance_sha=sha,
    )

    click.echo(json.dumps(verdict, indent=2))
    click.echo(f"\nAcceptance {acceptance_state.upper()} for {repo} #{issue_number} @ {sha}")

    if acceptance_state == "passed":
        _remove_acceptance_worktree(repo_dir, wt_path)
    else:
        click.echo(f"  worktree kept for inspection: {wt_path}")
        sys.exit(1)
