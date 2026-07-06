"""``coord acceptance`` — the framework-agnostic oracle-loop runner (#944,
docs/ORACLE_LOOP.md).

Subcommands:

- ``coord acceptance run --repo R (--issue N | --all)`` — run the repo's
  declared driver **in-session** (the worker's own warm loop) and print a
  structured pass/fail verdict. Sealed: verdicts only, never test source.
- ``coord acceptance record --repo R --issue N --sha SHA`` — the
  coordinator's **external** trust gate: re-run the sealed slice against the
  pushed SHA in a throwaway worktree and write the verdict to the board (the
  Acceptance box). Routes the whole command through the daemon (mirrors
  ``coord merge`` / ``coord diagnose`` — the no-local-DB rule), never a bare
  ``save_board``.
- ``coord acceptance mock <repo> <tracking_issue>`` — Gate A (#930): dispatch
  an independent mock-author that renders a viewable mock + writes
  ``tests/acceptance/ms-NN/contract.md``.
  ``coord.milestone_dispatch.gate_a_status`` blocks the milestone's issue
  dispatch until that contract exists.
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


@acceptance_group.command(
    "author",
    help=(
        "Dispatch an independent `type=\"test-author\"` session (#931, "
        "docs/ORACLE_LOOP.md) that authors — or, with --issue, extends — the "
        "sealed feature-level acceptance suite for a milestone from its "
        "Gate-A contract. TRACKING_ISSUE is the milestone's tracking issue "
        "number (same argument `coord milestone order`/`gate-c` take); the "
        "milestone number is resolved from it. Requires "
        "`tests/acceptance/ms-NN/contract.md` to already exist in the repo "
        "(hand-authored, or produced by the mock-author, #930) — the "
        "test-author reads it from its own checkout, it is not dispatched "
        "with the contract text embedded."
    ),
)
@click.argument("repo")
@click.argument("tracking_issue", type=int)
@click.option(
    "--issue", "issue_number", type=int, default=None,
    help=(
        "Scope to one issue's just-in-time slice instead of the whole "
        "milestone (must be a member of TRACKING_ISSUE's work order)."
    ),
)
@click.option(
    "--machine", "machine_override", default=None,
    help="Force a specific machine instead of auto-picking one.",
)
@_CONFIG_OPTION
def acceptance_author(
    repo: str,
    tracking_issue: int,
    issue_number: int | None,
    machine_override: str | None,
    config_path: Path,
) -> None:
    """Dispatch the independent test-author for REPO's milestone."""
    from coord.test_author import dispatch_test_author

    cfg = _load_config(config_path)
    try:
        assignment_id, machine_name = dispatch_test_author(
            repo,
            tracking_issue,
            cfg,
            issue_number=issue_number,
            machine_override=machine_override,
        )
    except RuntimeError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    scope = f"issue #{issue_number} slice" if issue_number is not None else "full milestone"
    click.echo(
        f"Dispatched test-author {assignment_id} to {machine_name} for "
        f"{repo} (tracking issue #{tracking_issue}, {scope})."
    )


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

    # #944 review: _scoped_verdict exits(1) internally for a manifest that
    # hasn't been authored yet / has no slice for this issue — a
    # configuration error, not a real (kept-for-inspection) test failure, so
    # the throwaway worktree must still be cleaned up on the way out.
    try:
        verdict = _scoped_verdict(result.tests, wt_path / ACCEPTANCE_DIRNAME, issue_number)
    except SystemExit:
        _remove_acceptance_worktree(repo_dir, wt_path)
        raise

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
        # Same rationale: a lookup error, not a failing-verdict "kept for
        # inspection" case — don't leak the worktree.
        _remove_acceptance_worktree(repo_dir, wt_path)
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
        # #932: per-test counts so the Acceptance box can show partial
        # progress ("3/7 acceptance green") instead of a bare verdict.
        acceptance_total=verdict["total"],
        acceptance_passed=verdict["passed"],
    )

    click.echo(json.dumps(verdict, indent=2))
    click.echo(f"\nAcceptance {acceptance_state.upper()} for {repo} #{issue_number} @ {sha}")

    if acceptance_state == "passed":
        _remove_acceptance_worktree(repo_dir, wt_path)
    else:
        click.echo(f"  worktree kept for inspection: {wt_path}")
        sys.exit(1)


@acceptance_group.command(
    "mock",
    help=(
        "Gate A (#930, docs/ORACLE_LOOP.md): dispatch an independent "
        "mock-author agent that renders a viewable mock of the milestone's "
        "user-facing surface and writes tests/acceptance/ms-NN/contract.md "
        "— the black-box contract the milestone's workers and the "
        "independent test-author (#931) implement/test to. REPO is the "
        "local repo name from coordinator.yml; TRACKING_ISSUE is the GH "
        "issue number of the milestone's tracking issue (must carry a "
        "milestone). `coord milestone dispatch` refuses this milestone's "
        "issues until the contract this produces exists."
    ),
)
@click.argument("repo")
@click.argument("tracking_issue", type=int)
@click.option(
    "--machine",
    default=None,
    help="Override machine selection (default: first idle machine that lists the repo).",
)
@_CONFIG_OPTION
def acceptance_mock_cmd(
    repo: str, tracking_issue: int, machine: str | None, config_path: Path
) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    from coord.mock_author import dispatch_acceptance_mock

    try:
        assignment_id, picked_machine = dispatch_acceptance_mock(
            repo,
            tracking_issue,
            cfg,
            machine_override=machine,
        )
    except RuntimeError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Dispatched mock-author for #{tracking_issue} -> {picked_machine}")
    click.echo(assignment_id)
