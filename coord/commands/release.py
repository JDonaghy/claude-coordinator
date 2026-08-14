"""`coord release-preflight` — local sanity checks before cutting a release.

#1471: `main` is now a protected branch, so a plain ``git push origin main``
can be silently *rejected* while a subsequent ``git push origin vX.Y.Z``
still *succeeds* — the two pushes are independent refs and nothing couples
them. That let a v0.4.82 release publish (immutably) to PyPI from a commit
that, at that moment, existed nowhere but the releaser's local checkout and
the tag.

This command is a fast, local, no-side-effects check meant to run right
before tagging a release, per the flow in docs/AGENT_OPERATIONS.md (merge PR
-> pull merged main -> tag -> push tag — #1238 dropped the version-bump step
that used to precede it: the git tag *is* the version now, single-sourced
via setuptools-scm). It does not push, tag, or modify anything itself.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import click

from coord.commands._common import _CONFIG_OPTION


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def release_preflight_checks(repo_root: Path) -> list[str]:
    """Return a list of problems with *repo_root* as a release candidate.

    Empty list == clear to tag. Kept as a pure(ish) function over a repo
    checkout — the only side effect is a ``git fetch origin main`` — so it's
    straightforward to unit test against local-only git fixtures (no real
    network) and to reuse outside the CLI command if needed.

    Checks, mirroring the issue's #1471 proposal:
    - working tree is clean (no staged/unstaged changes)
    - currently on ``main``, and local ``main`` == ``origin/main`` (the
      protected-branch push must have already landed via a merged PR)

    #1238: this used to also assert ``pyproject.toml``'s ``version`` and
    ``coord/__init__.py``'s ``__version__`` agreed, and that the version
    they named wasn't already tagged. Both checks are gone along with the
    hand-maintained version literals they compared — the version is now
    single-sourced from the git tag itself (setuptools-scm), so there is no
    bump left to forget or mismatch. Cutting a release is just choosing and
    pushing a ``vX.Y.Z`` tag that doesn't exist yet; ``git tag vX.Y.Z``
    itself already refuses a name collision, so a redundant check here would
    add nothing.
    """
    problems: list[str] = []

    if not (repo_root / ".git").exists():
        return [f"{repo_root} is not a git checkout"]

    status = _git(repo_root, "status", "--porcelain")
    if status.returncode != 0:
        problems.append(f"git status failed: {status.stderr.strip()}")
    elif status.stdout.strip():
        problems.append(
            "working tree is not clean — commit or stash changes before "
            "releasing:\n" + status.stdout.rstrip()
        )

    branch = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != "main":
        # #1525: this check fires by design on a `release-v*` bump branch —
        # this command is a post-merge, pre-tag check (see the module
        # docstring's flow), not something to run while the bump PR is still
        # open. Spell that out here since the bare "not on main" message
        # read as a bug the first time it fired on a release branch.
        problems.append(
            f"not on main (currently on '{branch}') — this is a post-merge, "
            "pre-tag check: merge the release PR first, then `git checkout "
            "main && git pull origin main` and re-run this from there"
        )

    fetch = _git(repo_root, "fetch", "origin", "main")
    if fetch.returncode != 0:
        problems.append(f"git fetch origin main failed: {fetch.stderr.strip()}")
    else:
        local_head = _git(repo_root, "rev-parse", "HEAD").stdout.strip()
        remote_head = _git(repo_root, "rev-parse", "origin/main").stdout.strip()
        if local_head and remote_head and local_head != remote_head:
            problems.append(
                f"local main ({local_head[:8]}) != origin/main ({remote_head[:8]}) — "
                "pull/rebase onto origin/main first. main is protected: your "
                "change must land there via a merged PR *before* you tag "
                "it (#1471) — a tag built from a commit main rejected still "
                "publishes to PyPI, and PyPI releases are immutable."
            )

    return problems


@click.command(
    "release-preflight",
    help="Sanity-check the checkout before cutting a release (#1471).",
)
@click.option(
    "--path",
    "path_opt",
    default=None,
    help="Repo checkout to check (defaults to the current directory).",
)
def release_preflight(path_opt: str | None) -> None:
    """Fail loudly, before any tag is pushed, if release ordering would be wrong.

    Run this right before ``git tag vX.Y.Z && git push origin vX.Y.Z``. It
    fetches ``origin/main`` and confirms local ``main`` matches it and the
    working tree is clean — so the #1471 failure mode (tagging a commit that
    never actually landed on the protected ``main`` branch) is caught
    locally instead of shipping an immutable bad PyPI release.
    """
    repo_root = Path(path_opt).expanduser() if path_opt else Path.cwd()
    problems = release_preflight_checks(repo_root)
    if problems:
        click.echo("release preflight FAILED:", err=True)
        for problem in problems:
            click.echo(f"  - {problem}", err=True)
        sys.exit(1)
    click.echo(
        "release preflight OK — local main matches origin/main, working "
        "tree clean. Ready to tag: git tag vX.Y.Z && git push origin vX.Y.Z."
    )


# ──────────────────────────────────────────────────────────────────────────
# `coord release verify` — the POST-release half (#1834)
# ──────────────────────────────────────────────────────────────────────────
#
# `release-preflight` above guards the moment *before* a tag is pushed. It
# says nothing about whether the release that came out the other end ever
# reached the fleet — and on 2026-08-04 it demonstrably had not, while four
# independent readouts said it had. See `coord/release_verify.py` for the
# incident and the design rules; this file only owns the click surface.
#
# `release-preflight` stays registered as a flat top-level command for
# backward compatibility (it is in every operator's muscle memory and in
# docs/AGENT_OPERATIONS.md); the new `release` group carries `verify`, and
# aliases `preflight` under it so the pair is discoverable together.


@click.group("release", help="Release lifecycle checks (#1471, #1834).")
def release_group() -> None:
    """Pre-tag sanity checks and post-release fleet verification."""


def _resolve_expected(expected: str | None, *, use_pypi: bool, index_url: str,
                      timeout: float) -> tuple[str | None, str | None]:
    """(expected version, warning) — the version every lane *should* be on.

    ``--expected`` wins outright. ``--pypi`` asks the simple index (never the
    JSON API — see ``coord.health.pypi`` for why that distinction is
    load-bearing rather than pedantic). With neither, there is no absolute to
    grade against and the command falls back to pure skew detection, which is
    what actually caught 2026-08-04: nobody knew what to expect, but two
    lanes disagreeing was already conclusive.
    """
    if expected:
        return expected.lstrip("v"), None
    if not use_pypi:
        return None, None
    from coord.health.pypi import latest_release_any  # noqa: PLC0415

    try:
        _project, latest, _all = latest_release_any(index_url=index_url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — read-only, degrade to skew-only
        return None, f"could not read the PyPI simple index ({exc}); checking skew only"
    if latest is None:
        return None, "PyPI simple index returned no release; checking skew only"
    return latest.raw, None


@release_group.command(
    "verify",
    help=(
        "Assert every deploy lane on every host actually reflects the "
        "released version (#1834). Read-only; safe to run mid-flight."
    ),
)
@_CONFIG_OPTION
@click.option(
    "--expected",
    default=None,
    help=(
        "The version every lane must be on (leading 'v' optional). Without "
        "it, the command reports skew BETWEEN lanes, which is what the "
        "2026-08-04 incident actually looked like."
    ),
)
@click.option(
    "--pypi/--no-pypi",
    "use_pypi",
    default=True,
    show_default=True,
    help=(
        "Resolve --expected from the PyPI simple index (the released "
        "version). On by default since #2052: without an expected version "
        "this command compares the fleet against ITSELF, so a fleet that is "
        "uniformly four releases behind reports crit=0."
    ),
)
@click.option("--machine", "machine_filter", default=None,
              help="Only poll this machine (still reports it as one lane set).")
@click.option("--timeout", default=5.0, show_default=True,
              help="Per-host HTTP timeout, seconds.")
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
@click.option("-v", "--verbose", is_flag=True, help="Show each lane's resolved path.")
@click.option(
    "--exit-code/--no-exit-code",
    default=True,
    show_default=True,
    help="Exit 2 on crit, 1 on warn/unknown (mirrors `coord health`).",
)
def release_verify(
    config_path: Path,
    expected: str | None,
    use_pypi: bool,
    machine_filter: str | None,
    timeout: float,
    as_json: bool,
    verbose: bool,
    exit_code: bool,
) -> None:
    """Post-release: prove the fleet is on the version you think it is.

    Runs entirely over HTTP — each machine's own ``/health`` plus the
    daemon's ``/board`` — so it works from a thin client with no checkout and
    no credentials, and it never writes anything anywhere.
    """
    import json as _json  # noqa: PLC0415

    from coord import release_verify as rv  # noqa: PLC0415
    from coord.commands._common import _load_config  # noqa: PLC0415

    config = _load_config(config_path)
    index_url = getattr(getattr(config, "health", None), "pypi_index_url",
                        "https://pypi.org/simple")
    resolved, warning = _resolve_expected(
        expected, use_pypi=use_pypi, index_url=index_url, timeout=timeout
    )
    if warning and not as_json:
        click.echo(f"warning: {warning}", err=True)

    machine_health, unreachable, daemon_host, daemon_name = rv.gather(
        config, timeout=timeout, machine_filter=machine_filter
    )
    report = rv.verify(
        machine_health=machine_health,
        unreachable=unreachable,
        daemon_host=daemon_host,
        daemon_host_name=daemon_name,
        expected=resolved,
    )

    if as_json:
        click.echo(_json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        click.echo(rv.render(report, verbose=verbose))

    if exit_code and report.exit_code:
        sys.exit(report.exit_code)


# ──────────────────────────────────────────────────────────────────────────
# `coord release propagate` — the PROPAGATE half (#1835, PKG-7)
# ──────────────────────────────────────────────────────────────────────────
#
# The I/O shell over `coord.release_propagate`. Everything that decides
# anything — is the fleet quiescent, in what order may lanes roll, which
# deploy gates a finished roll releases — lives in that module and is unit
# tested without a fleet. What lives here is the part that needs one:
# fetching the board, POSTing to agents, running the verifier, appending the
# journal.
#
# Publish and propagate are separate on purpose and the separation is the
# whole design: see `.github/workflows/auto-release.yml` and
# `coord/release_propagate.py`'s module docstring.


def _state_dir() -> Path:
    from coord.platform_paths import default_coord_dir  # noqa: PLC0415

    return default_coord_dir()


def _fetch_board() -> tuple[dict, str | None]:
    """``(board_payload, error)`` — never raises.

    A board this command cannot read is a *deferral*, not a crash: the
    propagation timer runs unattended, and an unreadable board means we
    cannot prove the fleet is idle, which is exactly the state in which the
    safe move is to do nothing and say so.
    """
    from coord import release_verify as rv  # noqa: PLC0415

    try:
        return rv._default_board_fetch() or {}, None
    except Exception as exc:  # noqa: BLE001 — see docstring
        return {}, f"{type(exc).__name__}: {exc}"


def _interactive_session_busy(config) -> list:
    """Live interactive tmux sessions as host-pinned `Busy` signals (#2228).

    `release_propagate.assess_quiescence`'s ``extra_busy`` seam was built
    for exactly this — its own docstring names "an interactive tmux
    session" as the seam's purpose — but until now nothing ever fed it one.
    An interactive session is invisible to the board *by construction*
    (``coord assign --interactive`` launches into tmux; the tmux session
    never POSTs ``/assign``), so without this a host running one reads as
    idle and rolls out from under a human, restarting `coord-serve` on the
    daemon host mid-session.

    Reuses the same discovery `coord sessions --remote` renders
    (:func:`coord.interactive.gather_fleet_tmux_sessions`) rather than
    inventing a second way to look.

    Fails OPEN, not closed: a probe error is logged and DROPPED, never
    turned into a host-less `Busy` — an unattributable signal blocks EVERY
    host by design (`Quiescence.fleet_wide_busy`), and an unreadable
    session list must not silently escalate into that.
    """
    from coord import release_propagate as rp  # noqa: PLC0415
    from coord.interactive import gather_fleet_tmux_sessions  # noqa: PLC0415

    try:
        sessions, errors = gather_fleet_tmux_sessions(config)
    except Exception as exc:  # noqa: BLE001 — fail open, see docstring
        click.echo(
            f"warning: could not probe interactive sessions ({exc}) — "
            "quiescence is blind to them this run",
            err=True,
        )
        return []

    for machine in errors:
        click.echo(
            f"warning: could not probe {machine} for live interactive "
            "sessions — quiescence is blind to that host this run",
            err=True,
        )

    busy = []
    for s in sessions:
        if s.get("pane_dead") == "1":
            continue  # claude exited; tmux is up but nobody is driving it
        machine = s.get("machine")
        if not machine:
            continue  # no coordinator.yml host to pin it to (#2228: never invent one)
        busy.append(
            rp.Busy(
                kind="interactive session",
                subject=f"{machine}:{s['session_name']}",
                detail="`coord assign --interactive` never POSTs /assign, "
                "so the board cannot see this",
                host=machine,
            )
        )
    return busy


def _daemon_machine_name(
    config, override: str | None, machine_health: dict | None = None
) -> str | None:
    """Which machine in ``coordinator.yml`` runs ``coord-serve``.

    The daemon must lead every roll (see :func:`coord.release_propagate.
    plan_lanes`), so getting this wrong is not cosmetic — it reintroduces
    the documented 405, and #2052 watched exactly that happen: a partial
    revert briefly left the daemon host on 0.5.4 while both callers sat on
    0.5.8, because nothing could name the daemon and the roll fell back to
    ``coordinator.yml`` order.

    Resolution order, derivation first and guesswork nowhere:

    1. the explicit ``--daemon-host`` flag;
    2. **derived** — the machine whose own ``/health`` reports a running
       ``coord-serve`` unit (:func:`coord.release_verify.
       daemon_host_from_health`). This is the fact itself, not a proxy for it;
    3. the host in the configured ``board_service`` URL matched against each
       machine's host — still derived, just from config rather than from the
       fleet;
    4. ``None``, which the caller treats as *refuse the run*. Ordering is the
       one thing protecting against the 405; a run that cannot order itself
       must stop, not roll in whatever order the file happens to list.
    """
    machines = list(getattr(config, "machines", ()) or ())
    if override:
        return override

    if machine_health:
        from coord.release_verify import daemon_host_from_health  # noqa: PLC0415

        derived = daemon_host_from_health(machine_health)
        if derived:
            return derived

    try:
        from urllib.parse import urlparse  # noqa: PLC0415

        from coord.client import resolve_board_service  # noqa: PLC0415

        svc = resolve_board_service()
        if svc is None:
            return None
        host = (urlparse(svc.url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return None
    if not host:
        return None
    for machine in machines:
        if str(getattr(machine, "host", "")).lower() == host:
            return machine.name
        if machine.name.lower() == host:
            return machine.name
    return None


def _post(url: str, payload: dict, *, timeout: float) -> tuple[int | None, dict, str]:
    """POST JSON, tolerantly. ``(status, body, error)``."""
    import httpx  # noqa: PLC0415

    try:
        resp = httpx.post(url, json=payload, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return None, {}, f"{type(exc).__name__}: {exc}"
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {}
    return resp.status_code, (body if isinstance(body, dict) else {}), ""


def _lane_versions_by_host(report) -> dict[str, list[str | None]]:
    out: dict[str, list[str | None]] = {}
    for lane in report.lanes:
        out.setdefault(lane.host, []).append(lane.version)
    return out


@release_group.command(
    "propagate",
    help=(
        "Roll the released version onto each host at ITS next quiescent "
        "window (#2067 — per host, not fleet-wide), verify it, and roll "
        "back on red (#1835). Safe to run from a timer: a busy host is a "
        "recorded deferral for that host, not a failure of the run."
    ),
)
@_CONFIG_OPTION
@click.option("--target", "target", default=None,
              help="Version to propagate (leading 'v' optional). Default: PyPI's latest.")
@click.option("--daemon-host", "daemon_host_override", default=None,
              help="Machine name running coord-serve. It rolls FIRST — a caller "
                   "must never reach an endpoint its daemon predates. Normally "
                   "DERIVED from the fleet's own /health; pass this when it "
                   "cannot be, since an unorderable multi-host run refuses.")
@click.option("--lane", "lane_filter", multiple=True,
              type=click.Choice(["python", "units", "tui"]),
              help="Only roll these lanes (repeatable). Default: all of them.")
@click.option("--dry-run", is_flag=True,
              help="Print the window verdict and the roll plan; change nothing.")
@click.option("--force", is_flag=True,
              help="Roll even if the fleet is busy. This KILLS in-flight headless "
                   "workers — the whole reason propagation is quiescence-scheduled.")
@click.option("--verify/--no-verify", "do_verify", default=True, show_default=True,
              help="Run `coord release verify` as the final gate.")
@click.option("--rollback-on-red/--no-rollback-on-red", default=True, show_default=True,
              help="Roll every updated host back to its previous venv generation "
                   "when verification comes back CRIT *on a lane this run could "
                   "actually roll* (#2052). Findings on lanes propagation has no "
                   "channel for are advisory and never trigger this.")
@click.option("--release-holds/--no-release-holds", "release_holds", default=True,
              show_default=True,
              help="After a VERIFIED roll, release the drive-queue deploy gates "
                   "(#1757) that were waiting for exactly this deploy.")
@click.option("--timeout", default=180.0, show_default=True,
              help="Seconds to wait for each agent to report the new version.")
@click.option("--cordon/--no-cordon", "do_cordon", default=True, show_default=True,
              help="#2101: stop each behind host from starting NEW work until it "
                   "is up to date, so it drains into a rollable state instead of "
                   "waiting for a window that never comes. In-flight work is "
                   "never killed. --no-cordon also CLEARS any cordon this "
                   "mechanism already set — turning it off must release the "
                   "fleet, not freeze it.")
@click.option("--cordon-after", default=None, type=int,
              help="Releases behind before a host is cordoned (default: 1, i.e. "
                   "any drift). Raise it if release cadence ever makes one "
                   "fleet drain per release too expensive — see #2101 trap F.")
@click.option("--cordon-ttl", default=None, type=float,
              help="Seconds a cordon stays effective without being renewed "
                   "(default 3600). This is what stops a run killed mid-drain "
                   "from cordoning the fleet forever.")
@click.option("--drain-deadline", default=None, type=float,
              help="Seconds a host may fail to drain before the cordon "
                   "escalates loudly (default 5400).")
@click.option("--json", "as_json", is_flag=True, help="Emit the propagation record as JSON.")
def release_propagate(  # noqa: PLR0912, PLR0915 — a pipeline; the decisions are elsewhere
    config_path: Path,
    target: str | None,
    daemon_host_override: str | None,
    lane_filter: tuple[str, ...],
    dry_run: bool,
    force: bool,
    do_verify: bool,
    rollback_on_red: bool,
    release_holds: bool,
    timeout: float,
    do_cordon: bool,
    cordon_after: int | None,
    cordon_ttl: float | None,
    drain_deadline: float | None,
    as_json: bool,
) -> None:
    """One propagation attempt. Exit 0 on deferral, 1 on red, 2 on rollback.

    #2067: the window is assessed PER HOST, not fleet-wide. A host with a
    live assignment or a running drive-queue entry defers on its own; the
    others roll and get verified this run regardless. The one case that
    still defers the whole run is the daemon host itself being occupied —
    every other host's python lane has to wait behind it (see
    ``coord/release_propagate.py``'s LANE ORDER section) — and the case a
    signal can't be pinned to any one host at all (an unreadable board), in
    which nothing can be proven safe to roll.

    #2052: the final gate is scoped to the lanes this run attempted and could
    have moved. Verify grades lanes propagation cannot roll — the operator's
    ``~/.coord-cli-venv`` and a remote ``coord-tui`` binary, currently — and
    holding a roll to those made every successful run red, which
    ``--rollback-on-red`` then reverted. Those findings are still reported and
    journalled in full; they are simply not evidence about *this* roll.

    #2069: the python lane's reach used to stop at ``coord-agent`` — a venv
    could swap cleanly while ``coord-serve`` kept serving the generation it
    started with, and this command still exited green. ``_roll_python`` now
    also restarts ``coord-serve``/``coord-web``/``coord-drive-queue`` on
    whichever host actually runs them, right after that host's own ``/update``
    lands, so the ``coord-serve process`` and ``<unit> spawns`` findings are
    graded like any other python-lane lane instead of being permanently
    advisory.

    #2101: this command no longer only WAITS for a window, it CREATES one.
    Every host that is behind the target is *cordoned* — no new agents route
    there, in-flight work is untouched — so it drains itself into a rollable
    state; the moment it is rolled it is uncordoned again. That is why the
    version sweep below happens BEFORE the "fleet is busy, defer" branch: a
    run that defers without cordoning is a run that will defer again in 20
    minutes for exactly the same reason, which is how the fleet sat eleven
    releases behind for a day with elitebook idle and rollable throughout.
    """
    import json as _json  # noqa: PLC0415
    import time  # noqa: PLC0415

    from coord import release_propagate as rp  # noqa: PLC0415
    from coord import release_verify as rv  # noqa: PLC0415
    from coord.commands._common import AGENT_PORT, _load_config  # noqa: PLC0415

    config = _load_config(config_path)
    state_dir = _state_dir()
    record = rp.PropagationRecord(started_at=time.time(), dry_run=dry_run)

    def _finish(status: str, exit_code: int = 0) -> None:
        record.status = status
        record.finished_at = time.time()
        if not dry_run:
            try:
                rp.append_record(state_dir, record)
                rp.trim_journal(state_dir)
            except OSError as exc:
                click.echo(f"warning: could not append the propagation journal: {exc}",
                           err=True)
        if as_json:
            click.echo(_json.dumps(record.to_dict(), indent=2, sort_keys=True))
        else:
            click.echo("\n".join(rp.render_record(record)))
        sys.exit(exit_code)

    # ── 1. what version are we propagating? ──────────────────────────────
    index_url = getattr(getattr(config, "health", None), "pypi_index_url",
                        "https://pypi.org/simple")
    resolved, warning = _resolve_expected(
        target, use_pypi=not target, index_url=index_url, timeout=10.0
    )
    if warning:
        click.echo(f"warning: {warning}", err=True)
    record.target_version = rp.normalize_version(resolved)
    if not record.target_version:
        record.error = (
            "could not resolve a target version — pass --target, or fix "
            "access to the PyPI simple index"
        )
        _finish(rp.STATUS_FAILED, 1)

    # ── 2. is there a window? (fleet + per-host, #2067) ──────────────────
    board, board_error = _fetch_board()
    extra_busy = []
    if board_error:
        extra_busy.append(
            rp.Busy(kind="board unreadable", subject="/board", detail=board_error)
        )
    # #2228: the board can't see an interactive session (no /assign POST) —
    # feed assess_quiescence's extra_busy seam from the same fleet-wide tmux
    # probe `coord sessions --remote` uses, so a live session defers a roll
    # exactly like a live headless assignment does.
    extra_busy.extend(_interactive_session_busy(config))
    quiescence = rp.assess_quiescence(
        queue_entries=board.get("drive_queue") or [],
        assignments=board.get("assignments") or [],
        issues=board.get("issues") or [],
        extra_busy=extra_busy,
    )
    record.quiescence = quiescence.to_dict()
    if quiescence.stale:
        # #2110: a `running` row this assessment could disprove (its issue is
        # merged/closed) — not a busy signal, but not silent either. Printed
        # unconditionally, not just under `--json`, so a plain journal read
        # shows the fleet self-corrected a stale row instead of that fact
        # only ever existing inside `record.quiescence["stale"]`.
        click.echo(
            "note: ignoring stale drive-queue row(s) whose issue already "
            f"landed: {', '.join(quiescence.stale)} (run `coord drive-queue "
            "tick --reconcile-only` to clear them for good)",
            err=True,
        )

    hosts = [m.name for m in (getattr(config, "machines", ()) or ())]
    busy_hosts = quiescence.busy_hosts()
    # #2067: a signal that cannot be pinned to a host (the board itself
    # unreadable, a drive-queue entry with no recorded launch host) has to
    # block every host — and so does every configured host individually
    # being occupied, which is the same outcome by a different route.
    fully_busy = bool(quiescence.fleet_wide_busy) or (
        bool(hosts) and busy_hosts.issuperset(hosts)
    )
    if quiescence.busy and force:
        click.echo(
            "warning: --force — rolling over a BUSY fleet; in-flight "
            f"headless workers will be killed ({quiescence.reason})",
            err=True,
        )
        busy_hosts = set()  # --force overrides per-host busyness too

    # ── 3. who still needs it, and in what order? ────────────────────────
    #
    # #2101: this sweep used to sit BELOW the "fleet fully busy → defer"
    # return, because a run that could roll nothing had nothing to learn from
    # it. That is no longer true: a busy fleet is exactly the fleet that needs
    # cordoning, and cordoning needs to know who is behind. The cost is one
    # `/health` sweep (10s, parallel) on a tick that would otherwise have
    # returned immediately — paid so a deferral can make the NEXT run
    # different from this one instead of repeating it forever.
    machine_health, unreachable, daemon_facts, daemon_label = rv.gather(
        config, timeout=10.0
    )
    before = rv.verify(
        machine_health=machine_health, unreachable=unreachable,
        daemon_host=daemon_facts, daemon_host_name=daemon_label,
        expected=record.target_version,
    )
    current = rp.hosts_already_current(_lane_versions_by_host(before), record.target_version)

    # ── 3b. cordon the hosts that are behind, so they DRAIN (#2101) ──────
    #
    # Before the deferral return below, on purpose: cordoning is the thing
    # that turns "no window" into "a window in a few minutes". A run that
    # defers without cordoning has changed nothing about why it deferred.
    record.cordons = _apply_cordons(
        hosts=hosts,
        report=before,
        target_version=record.target_version,
        busy_reasons={h: quiescence.busy_reason_for_host(h) for h in hosts},
        enabled=do_cordon,
        threshold=cordon_after,
        ttl_seconds=cordon_ttl,
        drain_deadline=drain_deadline,
        dry_run=dry_run,
    )

    if fully_busy and not force:
        # The single most important line in this command: a deferral is a
        # normal, recorded, exit-0 outcome. A timer that defers all night
        # must be visibly *working*, not visibly failing. #2101: and now it
        # has cordoned whatever is behind on the way past, so the next tick
        # meets a fleet that is actually draining.
        _finish(rp.STATUS_DEFERRED, 0)

    # #2101: resolved AFTER the deferral above, deliberately. This refusal is
    # about the ORDER a roll happens in; a run that is going to roll nothing
    # has no order to get wrong, and turning "the fleet is busy" into a
    # `failed` record (exit 1, systemd marks the unit failed) would teach an
    # operator to ignore the one signal that matters.
    daemon_name = _daemon_machine_name(config, daemon_host_override, machine_health)
    if daemon_name is None and len(hosts) > 1:
        # #2052 fault 2: this used to warn and roll in coordinator.yml order.
        # It then briefly put the daemon host BEHIND both its callers during a
        # partial revert — the documented 405 hazard the warning itself named.
        # Ordering is the one thing protecting against that, so an unorderable
        # run refuses. It is not a failure of the fleet, but it is a failure of
        # this run, and a recorded one.
        record.error = (
            "could not identify which machine runs coord-serve, and this "
            "fleet has more than one host — REFUSING to roll. The lane order "
            "(daemon first) is the only thing preventing the documented 405, "
            "and rolling in coordinator.yml order is a guess, not an order. "
            "Fix the daemon host's /health so its coord-serve unit is "
            "visible, or pass --daemon-host <machine>."
        )
        click.echo(f"error: {record.error}", err=True)
        _finish(rp.STATUS_FAILED, 1)

    # #2067: the daemon must lead every python-lane roll (see the module
    # docstring's LANE ORDER section) — if it is itself occupied and not
    # already on the target, nothing may roll ahead of it, because that
    # would put a caller on a newer `coord` than the daemon it talks to
    # (the documented 405). This is the one case a per-host window still
    # has to defer the WHOLE run rather than just skip the busy host.
    if daemon_name in busy_hosts and daemon_name not in current:
        _finish(rp.STATUS_DEFERRED, 0)

    still_busy = busy_hosts - set(current)
    rolls = rp.plan_lanes(
        daemon_host=daemon_name,
        hosts=hosts,
        lanes=lane_filter or rp.ALL_LANES,
        skip_hosts=set(current) | busy_hosts,
    )
    for host in current:
        record.lanes.append(
            {"lane": "-", "host": host, "ok": None,
             "detail": f"already on v{record.target_version}"}
        )
    for host in sorted(still_busy):
        # #2067: the whole point — a busy host defers on its own, it does
        # not hold every OTHER host hostage. A re-run resumes it, same as
        # an unreachable host or a failed daemon roll does today.
        record.lanes.append(
            {"lane": "-", "host": host, "ok": None,
             "detail": f"deferred — {quiescence.busy_reason_for_host(host)}"}
        )
    for host, reason in sorted(unreachable.items()):
        record.lanes.append(
            {"lane": "-", "host": host, "ok": False, "detail": f"unreachable: {reason}"}
        )

    if dry_run:
        for roll in rolls:
            record.lanes.append(
                {"lane": roll.lane, "host": roll.host, "ok": None,
                 "detail": f"would roll ({roll.rationale})"}
            )
        if rolls:
            _finish(rp.STATUS_ROLLED, 0)
        _finish(rp.STATUS_DEFERRED if still_busy else rp.STATUS_UP_TO_DATE, 0)

    if not rolls:
        _finish(rp.STATUS_DEFERRED if still_busy else rp.STATUS_UP_TO_DATE, 0)

    # ── 4. roll, in the planned order ────────────────────────────────────
    by_name = {m.name: m for m in (getattr(config, "machines", ()) or ())}
    updated_hosts: list[str] = []
    local_name = _local_machine_name(config)

    # #1835 review: plan_lanes() puts the daemon host's python lane first
    # specifically so "a caller must never reach an endpoint its daemon
    # predates" holds — but that is only true if a failure there actually
    # stops every other host's python lane from rolling forward. Without
    # this, a failed daemon roll left the loop free to advance every other
    # host to target_version anyway, reproducing the documented 405 skew
    # for the rest of this run (up to --timeout seconds per remaining
    # host) until the final `coord release verify` gate caught it — or,
    # with --no-verify, not at all. So this is an enforced precondition,
    # not just an ordering suggestion: once the daemon's own python roll
    # fails, every other host's python lane is skipped outright.
    #
    # #2095 review: this used to be set from `_roll_python`'s own overall
    # `ok`, which #2095 correctly made `False` whenever ANY restarted
    # sibling failed — including coord-web, which has nothing to do with the
    # 405 hazard this flag exists to prevent (that hazard is specifically
    # "a caller running ahead of a daemon whose coord-serve hasn't reached
    # target_version yet"). Reusing that aggregate here meant a coord-web-
    # only failure on the daemon host — coord-serve itself restarts and
    # reports target_version fine — would ALSO halt every other host's
    # python lane for the rest of the run: a materially larger blast radius
    # than before #2095, and exactly the shape of the 2026-08-10 incident
    # this issue is about (dellserver's coord-serve was fine; coord-web was
    # what failed). `_roll_python` now reports `serve_unit_ok` separately —
    # whether coord-serve ITSELF is confirmed on target_version — and that,
    # not the lane's own `ok`, is what decides this.
    daemon_python_failed = False

    for roll in rolls:
        machine = by_name.get(roll.host)
        if machine is None:
            record.lanes.append({"lane": roll.lane, "host": roll.host, "ok": False,
                                 "detail": "not in coordinator.yml"})
            continue
        if roll.host in unreachable:
            record.lanes.append({"lane": roll.lane, "host": roll.host, "ok": False,
                                 "detail": "skipped — host unreachable"})
            continue

        if (
            roll.lane == rp.LANE_PYTHON
            and daemon_python_failed
            and roll.host != daemon_name
        ):
            # Not a failure of *this* host — it was simply never attempted,
            # because attempting it would put it ahead of a daemon that
            # cannot yet serve it. A re-run after the daemon is fixed
            # should resume here, not treat this host as needing rollback.
            detail = (
                "not attempted — daemon host's python lane failed; rolling "
                "this host first would reproduce the 405 skew the lane "
                "order exists to prevent"
            )
            record.lanes.append({"lane": roll.lane, "host": roll.host, "ok": None,
                                 "detail": detail})
            click.echo(f"  · {roll.label}: {detail}")
            continue

        if roll.lane == rp.LANE_PYTHON:
            ok, detail, serve_unit_ok = _roll_python(
                machine, target_version=record.target_version,
                agent_port=AGENT_PORT, timeout=timeout, force=force,
            )
            if ok:
                updated_hosts.append(roll.host)
            elif roll.host == daemon_name and not serve_unit_ok:
                daemon_python_failed = True
        elif roll.lane == rp.LANE_UNITS:
            ok, detail = _roll_units(machine, agent_port=AGENT_PORT)
        else:
            ok, detail = _roll_tui(
                machine, target_version=record.target_version, local_name=local_name
            )

        # #2052: `ok is None` from a lane executor means "there is no channel
        # for this lane on this host" — not a failure, and emphatically not
        # something the post-roll gate may hold this run to. The remote
        # coord-tui binary is the canonical case: propagation itself reports
        # there is no remote install path, so counting its staleness as
        # grounds for rolling back a good python roll is a category error.
        entry = {"lane": roll.lane, "host": roll.host, "ok": ok, "detail": detail}
        if ok is None:
            entry["unrollable"] = True
        record.lanes.append(entry)
        click.echo(f"  {'·' if ok is None else ('✓' if ok else '✗')} "
                   f"{roll.label}: {detail}")

    # ── 4b. uncordon what just rolled, immediately (#2101) ───────────────
    #
    # Immediately, and not after the verify gate below: the host is on the
    # target version and its agent has re-execed, so there is nothing left to
    # drain for and every extra second of cordon is work the fleet is not
    # doing. If verification then comes back red and rolls the host back, the
    # NEXT run re-cordons it — one loop, converging, rather than a cordon
    # whose lifetime is coupled to an unrelated gate.
    _uncordon_hosts(updated_hosts, record.cordons)

    # ── 5. the final gate ────────────────────────────────────────────────
    if not do_verify:
        _finish(rp.STATUS_ROLLED, 0)

    machine_health, unreachable, daemon_facts, daemon_label = rv.gather(
        config, timeout=10.0
    )
    after = rv.verify(
        machine_health=machine_health, unreachable=unreachable,
        daemon_host=daemon_facts, daemon_host_name=daemon_label,
        expected=record.target_version,
    )
    record.verification = after.to_dict()

    # #2052: the gate is scoped to the lanes this run attempted and could
    # have moved. The full report above is still journalled verbatim — this
    # narrows what may TRIGGER a rollback, not what gets reported.
    gate = rp.scope_verification(record.verification, lanes=record.lanes)
    record.gate = gate.to_dict()
    for finding in gate.advisory:
        click.echo(
            f"  ~ advisory [{finding.get('severity')}] {finding.get('host')} "
            f"{finding.get('lane')}: {finding.get('summary')} "
            "— outside propagation's reach, fix by hand",
            err=True,
        )
    if gate.unrollable:
        click.echo(
            "  ~ lanes with no channel from this host: "
            + ", ".join(gate.unrollable),
            err=True,
        )

    if gate.red and rollback_on_red:
        # #1835: "a red post-deploy verification must roll back, not just
        # report." Only the hosts THIS run updated — rolling back a host we
        # never touched would undo somebody else's deliberate state.
        from coord.agent_update import cli_initiator  # noqa: PLC0415

        down: list[str] = []
        for host in updated_hosts:
            machine = by_name.get(host)
            if machine is None:
                continue
            ok, detail = _rollback_host(
                machine, agent_port=AGENT_PORT, timeout=min(timeout, 120.0),
                initiator=cli_initiator(
                    f"coord release propagate -> {machine.name} rollback (red gate)"
                ),
            )
            record.rolled_back.append(f"{host}: {detail}")
            if not ok:
                down.append(host)
            click.echo(f"  {'↩' if ok else '✗'} rollback {host}: {detail}")
        # #2052 fault 1: a rollback that stops a service and does not restore
        # it leaves the fleet WORSE off than the failed roll did — precision's
        # coord-agent sat `inactive (dead)` until a human noticed. If any host
        # did not come back, that is the headline, not a footnote.
        if down:
            record.error = (
                "ROLLBACK LEFT AGENTS DOWN: "
                + ", ".join(down)
                + " — these hosts answered the rollback but never came back "
                "on /health, and an SSH `systemctl --user restart "
                "coord-agent` did not revive them either. Recover by hand "
                "before anything else."
            )
            click.echo(f"error: {record.error}", err=True)
        _finish(rp.STATUS_ROLLED_BACK, 2)

    if gate.red:
        _finish(rp.STATUS_FAILED, 1)

    # ── 6. release the deploy gates that were waiting for this ───────────
    # Reaching this line means `after.severity != "crit"` — both crit
    # branches above already exit — so this roll is, definitionally, verified.
    for key in rp.holds_to_release(quiescence, verified=True):
        if not release_holds:
            click.echo(f"  · deploy gate {key} left held (--no-release-holds)")
            continue
        ok, detail = _release_hold(key)
        if ok:
            record.released_holds.append(key)
        click.echo(f"  {'✓' if ok else '✗'} release deploy gate {key}: {detail}")

    _finish(rp.STATUS_VERIFIED, 0)


# ── #2101: the cordon loop's I/O half ───────────────────────────────────────
#
# The decisions live in `coord/release_cordon.py` (pure, clock passed in);
# what lives here is the part that needs a fleet: reading each host's python
# lane out of the `/health` sweep this command already did, writing the
# daemon-backed cordon store, and surfacing a blown drain deadline where an
# operator will actually see it.


def _python_lane_versions(
    report, hosts: list[str], target_version: str | None
) -> dict[str, str | None]:
    """``{host: the OLDEST version its python lane reports}`` (#2101).

    "Python lane" is whatever :func:`coord.release_propagate.verify_lane_kind`
    grades as one — the venv itself plus the live `coord-serve` process —
    rather than a second list that could drift from the one the roll and the
    gate already use.

    Two deliberate readings:

    * the OLDEST readable version wins, because a host is as behind as its
      most stale python lane. A venv that swapped while `coord-serve` still
      runs the old generation is #2069's exact defect, and it must read as
      "behind", not as "done";
    * a host with an unreadable lane and no readable lane BEHIND the target is
      ``None`` — "no data", never "current" (#1834). `None` is what stops the
      host being cordoned on a guess *and* what stops an existing cordon being
      cleared on a failed HTTP call.
    """
    from coord import release_cordon as rc  # noqa: PLC0415
    from coord import release_propagate as rp  # noqa: PLC0415

    seen: dict[str, list[str | None]] = {}
    for lane in report.lanes:
        if rp.verify_lane_kind(lane.lane) != rp.LANE_PYTHON:
            continue
        seen.setdefault(lane.host, []).append(lane.version)

    out: dict[str, str | None] = {}
    for host in hosts:
        versions = seen.get(host) or []
        oldest = _oldest_version(versions)
        if (
            oldest is not None
            and any(v is None for v in versions)
            and rc.version_drift(oldest, target_version) == 0
        ):
            # Some lane could not be read at all, and every lane that COULD be
            # read is already on the target. That is not proof of "current" —
            # the unreadable lane is exactly the one #1834 says will be the
            # one that bites — so report "no data" and leave any existing
            # cordon exactly as it is. (A readable lane that IS behind is
            # proof enough to cordon, and `_oldest_version` already
            # surfaced it.)
            oldest = None
        out[host] = oldest
    return out


def _oldest_version(versions: list[str | None]) -> str | None:
    """The lowest readable version in *versions*, or ``None``.

    String comparison is wrong here (``0.5.9`` sorts above ``0.5.31``), so
    this compares numerically component by component.
    """
    def _key(raw: str) -> tuple[int, ...]:
        parts: list[int] = []
        for chunk in raw.lstrip("vV").split("."):
            digits = ""
            for ch in chunk:
                if not ch.isdigit():
                    break
                digits += ch
            if not digits:
                break
            parts.append(int(digits))
        return tuple(parts)

    readable = [v for v in versions if v]
    if not readable:
        return None
    return min(readable, key=_key)


def _apply_cordons(
    *,
    hosts: list[str],
    report,
    target_version: str | None,
    busy_reasons: dict[str, str],
    enabled: bool,
    threshold: int | None,
    ttl_seconds: float | None,
    drain_deadline: float | None,
    dry_run: bool,
) -> dict:
    """Plan and apply this run's cordons. Returns the journal fragment.

    Never raises: a cordon store this run cannot write is recorded as an
    error and the roll continues on its existing (quiescence-based) rules.
    Failing the whole propagation because the cordon could not be renewed
    would make #2101's fix strictly worse than not having it.
    """
    import time  # noqa: PLC0415

    from coord import release_cordon as rc  # noqa: PLC0415
    from coord.machine_pause import (  # noqa: PLC0415
        clear_cordon,
        cordons as read_cordons,
        set_cordon,
    )

    outcome = rc.CordonOutcome()
    try:
        existing = read_cordons()
    except Exception as exc:  # noqa: BLE001 — see docstring
        outcome.errors.append(f"could not read the cordon store: {exc}")
        return outcome.to_dict()

    plan = rc.plan_cordons(
        target_version=target_version,
        host_versions=_python_lane_versions(report, hosts, target_version),
        existing=existing,
        now=time.time(),
        ttl_seconds=(
            rc.DEFAULT_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        ),
        drain_deadline=(
            rc.DEFAULT_DRAIN_DEADLINE_SECONDS
            if drain_deadline is None
            else drain_deadline
        ),
        threshold=(
            rc.DEFAULT_DRIFT_THRESHOLD if threshold is None else threshold
        ),
        busy_reasons=busy_reasons,
        enabled=enabled,
    )
    for line in plan.render():
        click.echo(line)
    if dry_run:
        # `--dry-run` promises to change nothing, cordon store included. The
        # plan above is still printed, so a dry run answers "and what would
        # this do to the fleet's routing?" rather than going silent on it.
        outcome.errors.append("dry-run: cordon store not written")
        return {**plan.to_dict(), **outcome.to_dict()}

    outcome.expired = list(plan.expired)
    for record in plan.cordon:
        try:
            set_cordon(
                record.machine,
                reason=record.reason,
                target_version=record.target_version,
                ttl_seconds=max(0.0, record.expires_at - record.renewed_at),
            )
            outcome.cordoned.append(record.machine)
        except Exception as exc:  # noqa: BLE001 — see docstring
            outcome.errors.append(f"cordon {record.machine}: {exc}")
            click.echo(f"  ✗ cordon {record.machine}: {exc}", err=True)
    for name in plan.uncordon:
        try:
            if clear_cordon(name):
                outcome.uncordoned.append(name)
        except Exception as exc:  # noqa: BLE001 — see docstring
            outcome.errors.append(f"uncordon {name}: {exc}")
            click.echo(f"  ✗ uncordon {name}: {exc}", err=True)

    for escalation in plan.escalations:
        _escalate_drain(escalation)
        outcome.escalated.append(escalation.to_dict())

    return outcome.to_dict()


#: Where a blown drain deadline (#2101 trap C) is recorded, in the same
#: escalation channel `coord drive-queue`'s own alerts use — so it shows up in
#: the TUI's escalations panel and `coord drive escalations` with no new
#: surface to remember to look at. Mirrors
#: `coord.drive_queue.QUEUE_ALERT_REPO`'s pseudo-repo convention.
DRAIN_ALERT_REPO = "(release-cordon)"
DRAIN_ALERT_ISSUE = 0
DRAIN_ALERT_STAGE = "release-cordon"


def _escalate_drain(escalation) -> None:
    """Surface a host that will not drain — loudly, in three places.

    stderr (the timer's journal), the escalation table (the TUI and
    `coord drive escalations`) and the propagation journal. #2101's acceptance
    criterion 4 is explicitly about the surfaced MESSAGE rather than an
    internal state change, because a silent forever-wait is the failure this
    whole mechanism replaces.
    """
    click.echo(f"  ! {escalation.message}", err=True)
    try:
        from coord.state import record_drive_escalation  # noqa: PLC0415

        record_drive_escalation(
            DRAIN_ALERT_REPO,
            DRAIN_ALERT_ISSUE,
            stage=DRAIN_ALERT_STAGE,
            reason=escalation.message,
            gate_readings=(
                f"machine={escalation.machine} | "
                f"waited={escalation.waited_seconds:.0f}s | "
                f"deadline={escalation.deadline_seconds:.0f}s"
            ),
            proposed_command=escalation.command,
        )
    except Exception as exc:  # noqa: BLE001 — the stderr line above is the
        # floor; an escalation table that cannot be written must not take the
        # message down with it.
        click.echo(f"  (could not record the drain escalation: {exc})", err=True)


def _uncordon_hosts(hosts: list[str], journal: dict) -> None:
    """Clear the release cordon on every host in *hosts*, best effort."""
    if not hosts:
        return
    from coord.machine_pause import clear_cordon  # noqa: PLC0415

    for host in hosts:
        try:
            if clear_cordon(host):
                journal.setdefault("uncordoned", []).append(host)
                click.echo(f"  ✓ uncordon {host}: rolled, work may resume")
        except Exception as exc:  # noqa: BLE001
            journal.setdefault("errors", []).append(f"uncordon {host}: {exc}")
            click.echo(f"  ✗ uncordon {host}: {exc}", err=True)


def _local_machine_name(config) -> str | None:
    """This host's name in ``coordinator.yml``, if it is in there at all."""
    import socket  # noqa: PLC0415

    here = socket.gethostname().split(".")[0].lower()
    for machine in getattr(config, "machines", ()) or ():
        if machine.name.lower() == here:
            return machine.name
        if str(getattr(machine, "host", "")).split(".")[0].lower() == here:
            return machine.name
    return None


def _roll_python(machine, *, target_version: str, agent_port: int, timeout: float,
                 force: bool) -> tuple[bool, str, bool]:
    """POST /update and wait for the agent to actually report the version.

    Success is judged by the version the agent reports, never by "the POST
    was accepted" (#1568: a stale pip index makes a no-op look like a
    success) — the wait loop is ``coord agent update``'s own, reused rather
    than reimplemented so the two can't drift.

    Three-element return, ``(ok, detail, serve_unit_ok)`` (#2095 review):

    * ``ok`` is the whole lane's own verdict, exactly as before — #2095
      correctly made this ``False`` whenever ANY restarted sibling failed,
      coord-web included, so the lane never prints a `✓` over a real
      outage.
    * ``serve_unit_ok`` is narrower and answers a different question: is
      *coord-serve itself* — the unit whose version every other host's
      caller depends on, and the entire reason the main roll loop's
      ``daemon_python_failed`` cascade exists — confirmed to be on
      ``target_version`` and running? It is ``False`` only when the venv
      swap itself never completed (nothing downstream can be trusted
      either) or coord-serve was itself the sibling that failed to
      restart. A coord-web-only (or coord-drive-queue-only) failure
      leaves it ``True``. Reusing ``ok`` for that cascade decision used to
      mean a coord-web outage on the daemon host — coord-serve unaffected —
      also halted every other host's python lane for the rest of the run:
      a materially larger blast radius than before #2095, and exactly the
      2026-08-10 incident's shape (dellserver's coord-serve was fine;
      coord-web was what failed). Callers deciding whether it's safe to
      let OTHER hosts proceed must key off ``serve_unit_ok``, not ``ok``.
    """
    from coord.agent_update import cli_initiator  # noqa: PLC0415
    from coord.commands.agent_ops import (  # noqa: PLC0415
        _fetch_pre_started_at,
        _wait_agents_updated,
    )
    from coord.release_verify import DAEMON_UNIT  # noqa: PLC0415

    pre = _fetch_pre_started_at([machine])
    status, body, error = _post(
        f"http://{machine.host}:{agent_port}/update",
        {
            "target_version": target_version,
            "force": force,
            # #2121: the roll names itself on the target host's audit trail.
            "initiator": cli_initiator(
                f"coord release propagate -> {machine.name} python lane"
            ),
        },
        timeout=15.0,
    )
    if error:
        return False, error, False
    if status == 409:
        # The agent refused: live sessions, or an editable install. Both are
        # correct refusals and neither is this command's to override.
        return False, str(body.get("error") or "refused (409)"), False
    if status != 202:
        return False, f"HTTP {status}", False

    outcomes = _wait_agents_updated(
        [machine], target_version=target_version, timeout=timeout,
        pre_started_at=pre,
    )
    outcome = outcomes.get(machine.name) or {}
    if not outcome.get("matched"):
        return False, str(
            outcome.get("error")
            or f"still reporting v{outcome.get('version_now', '?')} after "
               f"{timeout:.0f}s (last update result: {outcome.get('result')})"
        ), False

    # #2069: /update only ever restarted the agent — coord-serve, coord-web
    # and coord-drive-queue kept running the generation they started with
    # until a human restarted them by hand. This is the rest of the lane,
    # not a separate one: it runs against whichever of those three units the
    # freshly-restarted agent finds actually running on ITS host, so a run
    # that never touches coord-web anywhere still reports "now vX.Y.Z" clean.
    sib_ok, sib_detail, sib_failed = _restart_sibling_services(machine, agent_port=agent_port)
    if sib_ok is not False:
        # True (nothing failed) and None (this agent predates
        # /restart-services entirely — no channel to have restarted anything
        # through, see `_restart_sibling_services`) both still count as a
        # lane success: neither one is a service THIS run took down.
        return True, f"now v{target_version}; {sib_detail}", True
    # #2095: this used to return `True` here too — "the venv swap succeeded"
    # bleeding into "the lane succeeded", printed as a leading `✓` over a
    # line that itself said `FAILED to restart: coord-web`. That is exactly
    # what happened during the 2026-08-10 0.5.15 -> 0.5.26 roll: the phone
    # dashboard went offline and the run reported success. The old comment
    # here claimed `coord release verify` would catch the resulting skew as
    # the justification for staying green — it does not: verify grades
    # *versions*, and there is no coord-web lane in it at all, so a dead
    # service is invisible to the thing named as its backstop. A sibling
    # that failed to (re)start — or restarted but never answered its own
    # liveness probe, see `agent_app._probe_liveness` — is not a lane
    # success, full stop, whatever the venv itself did.
    #
    # `sib_failed` is only populated when the endpoint told us per-unit
    # detail (a real 200/500-with-`units`-body); the opaque-failure branches
    # in `_restart_sibling_services` (a network error reaching the endpoint
    # at all, or a 500 with no body) return it empty because coord-serve's
    # own fate is genuinely unknown there — treated conservatively as NOT
    # confirmed, same as before #2095's per-unit distinction existed.
    serve_unit_ok = (DAEMON_UNIT not in sib_failed) if sib_failed else False
    return False, f"now v{target_version}, but {sib_detail}", serve_unit_ok


def _restart_sibling_services(
    machine, *, agent_port: int, timeout: float = 120.0
) -> tuple[bool | None, str, dict[str, str]]:
    """``POST /restart-services`` — the rest of a python-lane roll (#2069).

    ``/update`` swaps the venv and re-execs *the agent* — and nothing else.
    ``coord-serve``, ``coord-web`` and ``coord-drive-queue`` keep running the
    generation they started with until something restarts them, so this is
    called right after ``/update`` reports success above. Which of the three
    units actually need restarting is decided on the agent side, from what
    it finds running on its own host (see the endpoint's docstring) — this
    function only reports what came back.

    Three-element return: ``(ok, detail, failed)``. ``ok``/``detail`` follow
    the same tri-state convention as the other lane executors below
    (``_roll_units``/``_roll_tui``'s ``ok=None`` "no channel"):

    * ``True`` — the endpoint answered and no unit it touched failed.
    * ``False`` (#2095) — a sibling this run took down and never brought
      back: a real outage, not a cosmetic detail to carry forward under a
      `✓`. DOES fail the python lane — see ``_roll_python``. This used to
      defer to `coord release verify` catching the resulting skew; it
      cannot — verify grades versions, not liveness, and carries no lane
      for these units at all, so relying on it left exactly the outage this
      issue is about invisible to its own named backstop.
    * ``None`` — this host's agent predates the endpoint entirely (HTTP
      404): there is no channel here to have restarted anything through,
      the same "unrollable" shape as a lane with no executor at all, not a
      failure of this roll.

    ``failed`` (#2095 review) is the ``{unit: detail}`` mapping of units
    explicitly confirmed to have failed to restart — empty when ``ok`` is
    not ``False``, and ALSO empty for the opaque-failure branches below (a
    network error reaching the endpoint, or a 500 with no ``units`` body),
    where no individual unit's fate is actually known. ``_roll_python`` uses
    this — not the aggregate ``ok`` — to tell whether coord-serve itself is
    the sibling that failed, which is the only thing its ``serve_unit_ok``
    (and, through it, the main roll loop's daemon-python-failed cascade)
    cares about: a coord-web-only failure must not be indistinguishable from
    a coord-serve one to that caller.
    """
    status, body, error = _post(
        f"http://{machine.host}:{agent_port}/restart-services", {}, timeout=timeout,
    )
    if error:
        return False, f"sibling service restart: {error}", {}
    if status == 404:
        # Pre-#2069 agent build: /restart-services doesn't exist yet. Not
        # this run's failure to have restarted anything through a channel
        # that was never there — `coord agent update --all` is what closes
        # this gap, not a red python lane.
        return None, "agent predates /restart-services (HTTP 404) — update the agent build", {}
    # The endpoint (`agent_app.py`'s `restart_services`) returns HTTP 500 — with the
    # *same* `{"units": {...}}` body shape as 200 — whenever any single unit fails to
    # restart. That is the exact partial-failure path this function exists to report
    # in detail, so a 500 *with a `units` body* must still be parsed below rather than
    # treated as an opaque failure. A 500 WITHOUT a `units` body is a different,
    # genuinely-unexpected failure (an unhandled exception, a proxy error, ...) —
    # Starlette's own default error page carries no such body — and must still
    # short-circuit, or a real crash would be misread as "no sibling units to
    # restart" / a false-positive success.
    if status != 200 and not (status == 500 and "units" in body):
        return False, f"sibling service restart: {body.get('error') or f'HTTP {status}'}", {}

    units = body.get("units") or {}
    restarted = sorted(u for u, r in units.items() if isinstance(r, dict) and r.get("restarted"))
    skipped = sorted(
        u for u, r in units.items() if isinstance(r, dict) and r.get("restarted") is None
    )
    failed = {
        u: (r.get("detail") or "?")
        for u, r in units.items() if isinstance(r, dict) and r.get("restarted") is False
    }
    parts = []
    if restarted:
        parts.append(f"restarted {', '.join(restarted)}")
    if skipped:
        parts.append(f"not running here: {', '.join(skipped)}")
    if failed:
        parts.append(
            "FAILED to restart: "
            + ", ".join(f"{u} ({detail})" for u, detail in sorted(failed.items()))
        )
    if not parts:
        parts.append(body.get("detail") or "no sibling units to restart")
    return not failed, "; ".join(parts), failed


def _roll_units(machine, *, agent_port: int) -> tuple[bool | None, str]:
    """POST /deploy-units — the `deploy/**` lane's deploy step (#1831).

    Returns ``ok=None`` when this host offers no channel for the lane at all
    (see :func:`_roll_tui` and #2052): the run is not accountable for a lane
    it was structurally unable to roll.

    Also names every timer whose *enablement* state this call changed
    (#2124 item 3) — and, separately, every timer it deliberately left
    alone because it was already enabled (see
    :func:`coord.deploy_units.enable_timers`) — so "the queue came back
    mid-roll" (or, after this fix, "it didn't") is legible in this output
    rather than reconstructed afterwards from journal timestamps.

    ``ok`` (#2124 review) reflects every way ``agent_app.py``'s
    ``deploy_units`` endpoint can fail, not just a failed timer: a unit
    whose install itself failed (``action == "failed"`` — e.g. an unreadable
    installed unit file, :mod:`coord.deploy_units` lines ~249/264/284), a
    top-level ``error``, or a ``daemon-reload`` that was attempted and
    failed. Before this check existed, ``ok`` was derived from
    ``failed_timers`` alone, so any of those three could return HTTP 500
    with ``payload["ok"]=False`` and still be recorded here as a green
    lane — exactly the "unconfirmed success" defect #2096 exists to catch.
    """
    from coord.deploy_units import ACTION_FAILED  # noqa: PLC0415

    status, body, error = _post(
        f"http://{machine.host}:{agent_port}/deploy-units", {}, timeout=30.0
    )
    if error:
        return False, error
    if status in (404, 405):
        # Bootstrap: this agent predates the endpoint. It will have it after
        # the python lane above lands, so this is a *next run* fact, not a
        # failure — recorded rather than swallowed, and NOT grounds for the
        # gate to revert everything else this run got right (#2052).
        return None, ("agent has no /deploy-units yet (predates #1835) — "
                      "the next propagation will roll this lane")
    # The endpoint (`agent_app.py`'s `deploy_units`) returns HTTP 500 — with
    # the SAME body shape as 200 — whenever a daemon-reload or a single
    # timer's enablement fails. That is exactly the partial-failure path
    # this function exists to narrate in detail, so a 500 *with a `units`
    # body* must still be parsed below rather than treated as opaque. A 500
    # WITHOUT a `units` body is a different, genuinely-unexpected failure
    # (an unhandled exception, a proxy error, ...) and must still
    # short-circuit, or a real crash would be misread as "nothing to
    # report" / a false-positive success.
    if status != 200 and not (status == 500 and "units" in body):
        return False, str(body.get("error") or body.get("summary") or f"HTTP {status}")
    units = body.get("units") or []
    changed = [u.get("name") for u in units if u.get("action") == "updated"]
    new = [u.get("name") for u in units if u.get("action") == "new"]
    failed_units = {
        (u.get("name") or "?"): (u.get("detail") or "?")
        for u in units if u.get("action") == ACTION_FAILED
    }
    parts = []
    parts.append(f"{len(changed)} unit(s) refreshed" if changed else "units already current")
    if body.get("reloaded"):
        parts.append("daemon-reload ok")
    if new:
        parts.append(
            f"{len(new)} packaged unit(s) NOT installed here ({', '.join(sorted(map(str, new)))}) "
            "— a release does not decide which services a host runs"
        )
    if failed_units:
        parts.append(
            "FAILED to install unit(s): "
            + ", ".join(f"{u} ({detail})" for u, detail in sorted(failed_units.items()))
        )

    # A daemon-reload is only attempted when a unit's bytes actually
    # changed (`agent_app.py`'s `deploy_units`); "not reloaded" is a
    # failure only when the report itself says a reload was attempted
    # (`body["changed"]`, `InstallReport.changed`) — absent that, "not
    # reloaded" just means there was nothing to reload, never a failure.
    reload_failed = bool(body.get("changed")) and not body.get("reloaded")
    if reload_failed:
        parts.append(
            "daemon-reload FAILED: " + str(body.get("reload_detail") or "?")
        )

    top_level_error = body.get("error")
    if top_level_error:
        parts.append(f"deploy-units error: {top_level_error}")

    timers = body.get("timers_enabled") or {}
    started = sorted(
        name for name, r in timers.items()
        if isinstance(r, dict) and r.get("ok") and r.get("changed")
    )
    # Held timers (confirmed already-enabled, left alone) split on the
    # confirmed `ActiveState` carried in `enable_timers`'s own detail text
    # (coord/deploy_units.py's `already enabled (ActiveState=...)` branch)
    # — NOT on `changed` alone, which is equally true for the #2124 case
    # (operator stopped it) and for the routine, overwhelmingly common case
    # of a timer that was never touched because it is already enabled AND
    # already running. Reporting "left stopped as-is" for the latter names
    # a state (stopped) this call never confirmed — the #2124 review's
    # reporting-accuracy fix.
    held_stopped = []
    held_other = []
    for name, r in timers.items():
        if not (isinstance(r, dict) and r.get("ok") and not r.get("changed")):
            continue
        match = re.search(r"ActiveState=(\w+)", r.get("detail") or "")
        if match and match.group(1) == "inactive":
            held_stopped.append(name)
        else:
            held_other.append(name)
    held_stopped.sort()
    held_other.sort()
    failed_timers = {
        name: (r.get("detail") or "?")
        for name, r in timers.items() if isinstance(r, dict) and not r.get("ok")
    }
    if started:
        parts.append(f"enabled timer(s): {', '.join(started)}")
    if held_stopped:
        # Confirmed inactive (ActiveState=inactive) and deliberately left
        # alone — the #2124 fix in one word: a timer an operator stopped is
        # still here.
        parts.append(
            f"left stopped as-is (already enabled, #2124): {', '.join(held_stopped)}"
        )
    if held_other:
        # Already enabled and left alone, but NOT confirmed stopped — most
        # commonly a timer that is already enabled and already running
        # normally. Reported distinctly so it never gets read as "the
        # timer I stopped is still stopped" for a timer nobody stopped.
        parts.append(f"already enabled (unchanged): {', '.join(held_other)}")
    if failed_timers:
        parts.append(
            "FAILED to enable timer(s): "
            + ", ".join(f"{u} ({detail})" for u, detail in sorted(failed_timers.items()))
        )
    ok = not (failed_units or reload_failed or top_level_error or failed_timers)
    return ok, "; ".join(parts)


def _roll_tui(
    machine, *, target_version: str, local_name: str | None
) -> tuple[bool | None, str]:
    """`coord tui update` — local host only, and honest about the rest.

    ``coord-tui`` is a binary in each host's ``~/.local/bin``; there is no
    agent endpoint that installs it, so this lane can only roll where this
    command is running. Remote hosts are recorded as an explicit gap rather
    than silently omitted — a lane nobody can see is the lane that bites
    (#1834).

    #2052: that gap returns ``ok=None``, not ``ok=False``. It used to return
    False, which the post-roll gate then read as a failed lane and
    ``--rollback-on-red`` used as grounds to revert three good python rolls.
    A lane that reports "there is no remote install path" in its own failure
    message cannot also be evidence that this run went wrong.
    """
    import subprocess  # noqa: PLC0415

    if local_name is None or machine.name != local_name:
        return None, (
            f"coord-tui is a per-host binary with no remote install path — run "
            f"`coord tui update --version {target_version}` on {machine.name}"
        )
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "coord.cli", "tui", "update",
             "--version", target_version],
            capture_output=True, text=True, timeout=300,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode == 0:
        return True, f"coord-tui now v{target_version}"
    return False, (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:300]


def _get(url: str, *, timeout: float) -> tuple[int | None, dict]:
    """GET JSON, tolerantly. ``(status, body)`` — never raises."""
    import httpx  # noqa: PLC0415

    try:
        resp = httpx.get(url, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None, {}
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {}
    return resp.status_code, (body if isinstance(body, dict) else {})


def _wait_agent_back(machine, *, agent_port: int, timeout: float) -> tuple[bool, str]:
    """Poll ``/health`` until the agent answers again. ``(back, version)``.

    A rollback re-execs the agent process, and #2052 fault 1 is what happens
    when that re-exec does not take: precision's ``coord-agent`` went
    ``inactive (dead)`` at the moment of the rollback and stayed there until
    a human noticed. "The POST was accepted" is therefore not an outcome —
    the outcome is whether the service is serving again.
    """
    import time  # noqa: PLC0415

    deadline = time.time() + max(timeout, 1.0)
    poll = min(2.0, max(timeout / 10, 0.05))
    while True:
        status, body = _get(f"http://{machine.host}:{agent_port}/health", timeout=3.0)
        if status == 200:
            return True, str(body.get("version") or "?")
        if time.time() >= deadline:
            return False, "?"
        time.sleep(poll)


def _rollback_host(
    machine, *, agent_port: int, timeout: float = 90.0, initiator: str | None = None
) -> tuple[bool, str]:
    """POST /rollback — back to the previous blue/green generation (#1241) —
    and then put the service back on its feet.

    #2052 fault 1: "a rollback that stops a service and does not restore it
    leaves the fleet worse off than the failed roll did." This used to return
    True the instant the agent answered 202, which is a statement about the
    *request*, not about the host. It now waits for ``/health`` to answer
    again, escalates once to the documented SSH ``systemctl --user restart
    coord-agent`` (#404/#1568 — ``os.execv`` self-restart does not always
    take under systemd), and only then gives up — loudly, naming the host as
    DOWN rather than reporting a tidy "rolling back".

    #2121: *initiator* names this call on the target host's audit trail, the
    same as ``_roll_python``'s own ``/update`` POST a few lines up — both are
    automation that fires during a fleet roll with nobody at a keyboard, so
    both callers below build one with :func:`coord.agent_update.cli_initiator`
    rather than leaving it to the generic peer/user-agent fallback.
    """
    from coord.commands.agent_ops import _escalate_restart  # noqa: PLC0415

    status, body, error = _post(
        f"http://{machine.host}:{agent_port}/rollback",
        {"force": True, "initiator": initiator} if initiator else {"force": True},
        timeout=30.0,
    )
    if error:
        return False, error
    if status == 404:
        return False, "no previous generation on this host"
    if status != 202:
        return False, str(body.get("error") or f"HTTP {status}")

    back, version = _wait_agent_back(machine, agent_port=agent_port, timeout=timeout)
    if back:
        return True, f"rolled back; agent is serving again on v{version}"

    # The re-exec did not take. This is the documented systemd stall, and it
    # has a documented fix — apply it rather than handing the operator a
    # dead host and a tidy success message.
    escalated = _escalate_restart(machine)
    if escalated:
        back, version = _wait_agent_back(
            machine, agent_port=agent_port, timeout=min(timeout, 60.0)
        )
        if back:
            return True, (
                f"rolled back; agent needed an SSH `systemctl --user restart "
                f"coord-agent` but is serving again on v{version}"
            )
    return False, (
        "rolled back the venv but the agent is DOWN — it never came back on "
        f"/health within {timeout:.0f}s and "
        + (
            "the SSH restart did not revive it"
            if escalated
            else "the SSH `systemctl --user restart coord-agent` escalation "
            "could not run"
        )
        + f". Recover by hand on {machine.name}."
    )


def _release_hold(key: str) -> tuple[bool, str]:
    """``coord drive-queue resume REPO ISSUE`` — the gate the deploy was for.

    The queue's own command takes the pair, not the ``repo#issue`` key, so
    the key is split here rather than a second spelling of "resume this
    gate" being invented alongside it.
    """
    import subprocess  # noqa: PLC0415

    from coord.drive_queue import parse_key  # noqa: PLC0415

    parsed = parse_key(key)
    if parsed is None:
        return False, f"unparseable queue key {key!r}"
    repo, issue = parsed
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "coord.cli", "drive-queue", "resume",
             repo, str(issue)],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode == 0:
        return True, "queue released"
    return False, (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:200]


@release_group.command(
    "cordon",
    help=(
        "Inspect, set or clear release cordons (#2101). A cordoned machine "
        "starts no NEW work — in-flight work is never touched — so it drains "
        "into a state where `coord release propagate` can roll it. "
        "`coord release propagate` manages these automatically; this command "
        "is the operator's window into them and the documented override for a "
        "host that will not drain."
    ),
)
@click.argument("machines", nargs=-1)
@click.option("--clear", "clear", is_flag=True,
              help="Clear the named machines' cordons (or --all of them). "
                   "This lets work resume and LEAVES THE HOST BEHIND — the "
                   "next propagate run will cordon it again unless whatever "
                   "was wedged has been fixed.")
@click.option("--all", "all_machines", is_flag=True,
              help="With --clear: clear every release cordon.")
@click.option("--reason", default="", help="Free text stored with the cordon.")
@click.option("--target", "target_version", default=None,
              help="Version this cordon is draining for; shown in every "
                   "surface that renders the cordon.")
@click.option("--ttl", default=None, type=float,
              help="Seconds before the cordon lapses on its own if nothing "
                   "renews it (default 3600) — see #2101. That bounds a "
                   "propagate run that crashed mid-drain; it does NOT bound "
                   "a healthy one. `coord release propagate` renews this on "
                   "every tick a host is still behind, so a driven issue "
                   "stuck in a long fix loop can hold a cordon for as long "
                   "as that loop runs — see the drain-deadline escalation "
                   "(`coord release propagate --drain-deadline`, #2136) for "
                   "the case this TTL does not cover.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def release_cordon(
    machines: tuple[str, ...],
    clear: bool,
    all_machines: bool,
    reason: str,
    target_version: str | None,
    ttl: float | None,
    as_json: bool,
) -> None:
    """List (no args), set, or clear release cordons.

    Every cordon carries an owner, a reason, a creation time and an expiry,
    and is stored separately from `coord pause` — so this command can never
    clear a pause an operator set by hand, and `coord unpause` can never lift
    a cordon out from under a drain (#2101 trap A).
    """
    import json as _json  # noqa: PLC0415
    import time  # noqa: PLC0415

    from coord import release_cordon as rc  # noqa: PLC0415
    from coord.machine_pause import (  # noqa: PLC0415
        clear_cordon,
        cordons as read_cordons,
        set_cordon,
    )

    if clear:
        targets = list(machines)
        if all_machines:
            targets = sorted(read_cordons())
        if not targets:
            raise click.ClickException(
                "name at least one machine, or pass --all"
            )
        cleared = [name for name in targets if clear_cordon(name)]
        if as_json:
            click.echo(_json.dumps({"cleared": cleared}, indent=2, sort_keys=True))
        elif cleared:
            click.echo("uncordoned: " + ", ".join(cleared))
            click.echo(
                "note: these hosts are still BEHIND the released version — "
                "the next `coord release propagate` will cordon them again "
                "unless the thing that stopped them draining is fixed."
            )
        else:
            click.echo("nothing to do — none of those machines was cordoned")
        return

    if machines:
        written = [
            set_cordon(
                name,
                reason=reason or "cordoned by hand",
                target_version=target_version,
                ttl_seconds=ttl,
            )
            for name in machines
        ]
        if as_json:
            click.echo(
                _json.dumps([c.to_dict() for c in written], indent=2, sort_keys=True)
            )
        else:
            for record in written:
                click.echo(f"⊘ {record.machine}: {record.describe()}")
        return

    now = time.time()
    active = read_cordons(now=now)
    if as_json:
        click.echo(
            _json.dumps(
                [c.to_dict() for _, c in sorted(active.items())],
                indent=2,
                sort_keys=True,
            )
        )
        return
    if not active:
        click.echo("no machines are cordoned — the fleet is free to take work")
        return
    for name, record in sorted(active.items()):
        remaining = max(0.0, record.expires_at - now) / 60.0
        overdue = " OVERDUE" if record.overdue(now) else ""
        click.echo(
            f"⊘ {name}: {record.describe()} "
            f"[{record.age(now) / 60.0:.0f}m draining, lapses in "
            f"{remaining:.0f}m, owner={record.owner}]{overdue}"
        )
    click.echo(
        f"\nclear one with `coord release cordon --clear <machine>` "
        f"(default lifetime {rc.DEFAULT_TTL_SECONDS / 60:.0f}m — a cordon "
        "nobody renews lapses on its own)"
    )


@release_group.command(
    "rollback",
    help=(
        "ONE command that puts every agent back on its previous venv "
        "generation (#1241/#1560). The escape hatch for a bad release."
    ),
)
@_CONFIG_OPTION
@click.option("--machine", "machine_filter", default=None, help="Only this machine.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--wait", default=90.0, show_default=True,
              help="Seconds to wait for each agent to start serving again "
                   "before escalating to an SSH restart (#2052).")
def release_rollback(config_path: Path, machine_filter: str | None, yes: bool,
                     wait: float) -> None:
    """#1560 requires rollback to be one command, not a runbook.

    Every successful ``/update`` leaves the previous generation on disk
    (``coord.agent_update``'s two fixed blue/green slots) precisely so this
    can exist. It force-rolls: a rollback is what you reach for when the
    fleet is broken, and refusing because a worker is running on a broken
    release would be the wrong tradeoff at exactly the wrong moment.
    """
    from coord.agent_update import cli_initiator  # noqa: PLC0415
    from coord.commands._common import AGENT_PORT, _load_config  # noqa: PLC0415

    config = _load_config(config_path)
    machines = [
        m for m in (getattr(config, "machines", ()) or ())
        if not machine_filter or m.name == machine_filter
    ]
    if not machines:
        click.echo("no machines to roll back", err=True)
        sys.exit(2)
    if not yes:
        click.confirm(
            f"Roll back {len(machines)} agent(s) to the previous venv generation "
            "and restart them (this kills any in-flight worker)?",
            abort=True,
        )
    failures = 0
    for machine in machines:
        ok, detail = _rollback_host(
            machine, agent_port=AGENT_PORT, timeout=wait,
            initiator=cli_initiator(f"coord release rollback -> {machine.name}"),
        )
        click.echo(f"  {'↩' if ok else '✗'} {machine.name}: {detail}")
        failures += 0 if ok else 1
    if failures:
        sys.exit(1)


@release_group.command(
    "history",
    help="What propagation actually did, and when (#1835's observability gate).",
)
@click.option("--limit", default=40, show_default=True,
              help="Show at most this many recorded attempts (most recent last).")
@click.option("-v", "--verbose", is_flag=True,
              help="Show every no-op attempt individually instead of collapsing runs.")
@click.option("--json", "as_json", is_flag=True, help="Emit the raw records as JSON.")
def release_history(limit: int, verbose: bool, as_json: bool) -> None:
    """Read the propagation journal.

    #1835: "a silent success is indistinguishable from a silent no-op, which
    is precisely how 2026-08-04 stayed invisible." Every attempt is
    journalled, including the deferrals — so an empty history means the
    timer never ran, which is itself the finding.
    """
    import json as _json  # noqa: PLC0415

    from coord import release_propagate as rp  # noqa: PLC0415

    records = rp.read_records(_state_dir(), limit=limit)
    if as_json:
        click.echo(_json.dumps(records, indent=2, sort_keys=True))
        return
    click.echo(rp.render_history(records, verbose=verbose))


# ── #2112: the nightly daemon-host release window ───────────────────────────
#
# `coord release propagate` waits for a quiescent window that may never
# arrive on the daemon host specifically, because it both leads every roll
# (the documented 405) and is the box that launches drive-queue work, so
# almost any drive anywhere keeps it "busy" and defers the entire fleet
# (see `coord/release_window.py`'s module docstring for the full mechanism
# and the 2026-08-10 measurement). This section is the I/O shell over that
# module's decisions: stop the queue timer, drain in-flight drives bounded
# by a deadline, roll via `coord release propagate`, and ALWAYS restart the
# timer — the pure judgement (`needs_roll`, the journal, the record shape)
# lives in `coord/release_window.py`, same split as `release_propagate`'s
# own I/O shell above.
#
# GATED ON #2110: the drain loop below calls `coord drive-queue tick
# --reconcile-only` on every poll specifically because stopping the timer
# also stops the ONLY thing that otherwise reconciles a finished drive's row
# from `running` to `done` — without #2110 making that reconciliation safe to
# call standalone, this whole mechanism would deadlock every night.


def _systemctl(unit: str, action: str, *, runner=None, timeout: float = 30.0) -> tuple[bool, str]:
    """``systemctl --user <action> <unit>``, tolerantly.

    Mirrors `coord.deploy_units.enable_timers`'s own systemctl wrapper: same
    injectable ``runner`` seam for tests (a fleet-free unit test never
    spawns a real ``systemctl``), same "never raise" contract — a window run
    must be able to report "could not stop the queue" as a normal, recorded
    outcome rather than crash.
    """
    import subprocess  # noqa: PLC0415

    run = runner or subprocess.run
    try:
        proc = run(
            ["systemctl", "--user", action, unit],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return False, "systemctl not found (no systemd on this host)"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    ok = getattr(proc, "returncode", 1) == 0
    detail = (getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "").strip()
    return ok, detail or (f"{action} ok" if ok else f"{action} failed")


def _run_reconcile_tick(config_path: Path, *, runner=None) -> tuple[bool, str]:
    """``coord drive-queue tick --reconcile-only`` — best effort, #2110.

    Stopping `coord-drive-queue.timer` for the drain also stops the ONLY
    thing that otherwise reconciles a finished drive's row from ``running``
    to ``done``. Without calling this on every poll, a drive that finished
    *after* the timer stopped would stay stuck ``running`` for the rest of
    the drain no matter how generous the deadline is — the exact deadlock
    #2110 fixed reconciliation to no longer require the timer for.
    """
    import subprocess  # noqa: PLC0415

    run = runner or subprocess.run
    try:
        proc = run(
            [sys.executable, "-m", "coord.cli", "drive-queue", "tick",
             "--reconcile-only", "--config", str(config_path)],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as exc:  # noqa: BLE001 — best effort; the quiescence
        # check right after this call is the real signal a stuck reconcile
        # eventually clears on a later poll, not this one call's job to
        # guarantee.
        return False, f"{type(exc).__name__}: {exc}"
    ok = getattr(proc, "returncode", 1) == 0
    detail = (getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "").strip()
    return ok, detail[:200]


def _drain(
    *,
    daemon_host: str,
    config_path: Path,
    deadline: float,
    poll_interval: float,
    config=None,
    reconcile=None,
    board_fetch=None,
    extra_busy_fetch=None,
    now=None,
    sleep=None,
):
    """Bounded wait for *daemon_host* to stop being busy (trap 2).

    Reuses `release_propagate.assess_quiescence` — the SAME computation
    `coord release propagate` itself uses to decide whether the daemon host
    may lead a roll — rather than a second definition of "busy" (#2096's
    "two surfaces, one function" rule). ``now``/``sleep`` are injectable so
    this loop is unit-testable without a real clock; ``reconcile``/
    ``board_fetch``/``extra_busy_fetch`` default to the real subprocess/
    board/tmux-probe calls.

    #2228: ``extra_busy_fetch`` re-probes live interactive sessions on
    every poll (not just once) — a session that ends mid-drain should let
    the daemon host clear within THIS wait, not only on the next `coord
    release propagate` tick. Deliberately keyed off the explicit *config*
    param, never an implicit ``_load_config(config_path)`` fallback: this
    function is called with ``config_path=None`` from unit tests that
    inject their own clock/board — silently resolving a real
    ``coordinator.yml`` (and then SSH-probing whatever machines it names)
    the moment ``config`` is omitted would make this loop non-hermetic by
    default. No *config* simply means "no interactive-session signal this
    call" — the caller opts in by passing one, as the real
    ``coord release nightly-window`` call site does.
    """
    import time as _time  # noqa: PLC0415

    from coord import release_propagate as rp  # noqa: PLC0415
    from coord import release_window as rw  # noqa: PLC0415

    now_fn = now or _time.time
    sleep_fn = sleep or _time.sleep
    reconcile_fn = reconcile or (lambda: _run_reconcile_tick(config_path))
    fetch_fn = board_fetch or _fetch_board
    if extra_busy_fetch is None:
        if config is not None:
            extra_busy_fetch = lambda: _interactive_session_busy(config)  # noqa: E731
        else:
            extra_busy_fetch = lambda: []  # noqa: E731

    start = now_fn()
    while True:
        reconcile_fn()
        board, board_error = fetch_fn()
        extra_busy = []
        if board_error:
            extra_busy.append(
                rp.Busy(kind="board unreadable", subject="/board", detail=board_error)
            )
        extra_busy.extend(extra_busy_fetch())
        quiescence = rp.assess_quiescence(
            queue_entries=board.get("drive_queue") or [],
            assignments=board.get("assignments") or [],
            issues=board.get("issues") or [],
            extra_busy=extra_busy,
        )
        busy = bool(quiescence.fleet_wide_busy) or daemon_host in quiescence.busy_hosts()
        elapsed = now_fn() - start
        if not busy:
            return rw.DrainOutcome(drained=True, elapsed_seconds=elapsed, detail="drained")
        detail = quiescence.busy_reason_for_host(daemon_host) or quiescence.reason
        if elapsed >= deadline:
            return rw.DrainOutcome(drained=False, elapsed_seconds=elapsed, detail=detail)
        sleep_fn(max(0.0, min(poll_interval, deadline - elapsed)))


def _parse_trailing_json(stdout: str) -> dict | None:
    """The last top-level JSON object printed to *stdout*, or ``None``.

    #2187: this used to look only at the LAST LINE of stdout and try to
    parse THAT LINE alone as JSON. `coord release propagate --json` (like
    every ``--json`` command in this codebase) prints its record via
    ``json.dumps(..., indent=2, ...)`` — pretty-printed, for a human running
    it by hand — which means the payload spans many lines and its own last
    line is just ``}``, never a complete object on its own. The old check
    therefore never found the record for ANY propagate run, successful or
    not, and every call fell back to the ``f"exit {code}"`` placeholder in
    `_run_propagate` below — which is exactly the false "propagate-failed
    (status=exit 0, exit=0)" #2187 reports for a verified, exit-0 roll.

    This instead finds the LAST line that is the object's own unindented
    ``{`` (json.dumps never indents the outermost brace) and parses
    everything from there to the end of stdout as one document. A single
    compact line (no ``indent=``) still works too, tried second.
    """
    import json as _json  # noqa: PLC0415

    text = stdout.strip()
    if not text:
        return None
    lines = text.splitlines()
    for idx in range(len(lines) - 1, -1, -1):
        if lines[idx].strip() != "{":
            continue
        try:
            payload = _json.loads("\n".join(lines[idx:]))
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    for line in reversed(lines):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = _json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _latest_propagate_record_since(state_dir: Path, since: float) -> dict | None:
    """The propagation journal's own record for the run started at/after
    *since*, if any — the ground truth `coord release history` itself reads.

    #2187 proposal 1: "read `coord release history` for the run it just
    launched rather than inferring from a side-channel [stdout]." The
    journal has no request id to join on, so this uses ``started_at``: the
    subprocess `_run_propagate` shells out to is synchronous, so by the
    time it has returned, a successful run has already appended its record
    with a ``started_at`` at or after the moment this function's caller
    launched it. Picks the OLDEST such record — the first thing written
    after launch — in case some unrelated `coord release propagate` run
    elsewhere raced it onto the same shared journal.
    """
    from coord import release_propagate as rp  # noqa: PLC0415

    candidates = [
        rec for rec in rp.read_records(state_dir)
        if isinstance(rec.get("started_at"), (int, float)) and rec["started_at"] >= since
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda rec: rec["started_at"])


def _run_propagate(
    *, daemon_host: str, target_version: str, config_path: Path, state_dir: Path,
    runner=None, now_fn=None,
) -> tuple[str, int, str, float | None]:
    """``coord release propagate --daemon-host ... --target ... --json``.

    A real subprocess of THIS interpreter (matches `_roll_tui`/
    `_release_hold` above), not an in-process call: `release_propagate` is a
    click command that calls ``sys.exit()`` itself and is not meant to be
    invoked as a plain function. ``--target`` is passed explicitly — not
    left to re-resolve PyPI's "latest" a second time — so the version this
    run decided was needed is the version that actually rolls, even if a new
    release lands on PyPI mid-drain.

    Returns ``(status, exit_code, combined_output, propagate_started_at)``.
    ``status`` is read from the propagation journal entry that run itself
    wrote (:func:`_latest_propagate_record_since`) — the same ground truth
    `coord release history` reads — not reparsed from stdout (#2187:
    reparsing stdout was the false-negative bug). ``propagate_started_at``
    is that record's own ``started_at``, stamped onto the window's own
    record so `window-history` can be joined to `history` (#2187 proposal
    2) instead of the two stores re-deciding the same outcome separately.
    Only when no journal entry can be found — the subprocess died before
    writing one, or the journal write itself failed — does this fall back
    to `_parse_trailing_json` on stdout, and then to ``f"exit {code}"``;
    either fallback returns ``None`` for ``propagate_started_at`` since
    there is then nothing to join to.
    """
    import subprocess  # noqa: PLC0415
    import time  # noqa: PLC0415

    run = runner or subprocess.run
    clock = now_fn or time.time
    launched_at = clock()
    argv = [
        sys.executable, "-m", "coord.cli", "release", "propagate",
        "--daemon-host", daemon_host, "--target", target_version,
        "--json", "--config", str(config_path),
    ]
    try:
        proc = run(argv, capture_output=True, text=True, timeout=1800)
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"
        return f"error: {detail}", 1, detail, None
    code = getattr(proc, "returncode", 1)
    stdout = getattr(proc, "stdout", "") or ""
    stderr = getattr(proc, "stderr", "") or ""
    output = (stdout + ("\n" + stderr if stderr else "")).strip()

    record = _latest_propagate_record_since(state_dir, launched_at)
    if record is not None:
        status = str(record.get("status") or f"exit {code}")
        return status, code, output, record.get("started_at")

    payload = _parse_trailing_json(stdout)
    status = (
        str(payload["status"]) if payload and payload.get("status") else f"exit {code}"
    )
    return status, code, output, None


#: Where a skipped/failed nightly window (trap 3) is recorded — same
#: escalation channel #2101's drain-deadline escalation and `coord
#: drive-queue`'s own alerts use, so it shows up in the TUI's escalations
#: panel and `coord drive escalations` with no new surface to remember to
#: look at. Mirrors `_escalate_drain`'s `DRAIN_ALERT_*` convention above.
WINDOW_ALERT_REPO = "(release-window)"
WINDOW_ALERT_ISSUE = 0
WINDOW_ALERT_STAGE = "release-window"


def _escalate_window(record, *, reason: str) -> None:
    """Surface a skipped/failed nightly window loudly (trap 3).

    #2112's whole point: a night propagation was supposed to happen and did
    not is exactly the state #2082 exists to make loud elsewhere in this
    fleet. Silence must not be the report here either.
    """
    click.echo(f"  ! {reason}", err=True)
    try:
        from coord.state import record_drive_escalation  # noqa: PLC0415

        proposed = (
            f"coord release propagate --daemon-host {record.daemon_host} "
            f"--target {record.target_version}"
            if record.daemon_host and record.target_version
            else "coord release nightly-window --dry-run   # investigate first"
        )
        record_drive_escalation(
            WINDOW_ALERT_REPO, WINDOW_ALERT_ISSUE, stage=WINDOW_ALERT_STAGE,
            reason=reason,
            gate_readings=(
                f"daemon_host={record.daemon_host} | target={record.target_version} | "
                f"daemon_version={record.daemon_version} | status={record.status}"
            ),
            proposed_command=proposed,
        )
    except Exception as exc:  # noqa: BLE001 — the stderr line above is the
        # floor; an escalation table that cannot be written must not take
        # the message down with it.
        click.echo(f"  (could not record the window escalation: {exc})", err=True)


@release_group.command(
    "nightly-window",
    help=(
        "Guarantee the daemon host rolls at a nightly window instead of "
        "waiting for a fleet-wide quiescent moment that may never come "
        "(#2112). Stops the drive queue, drains in-flight drives (bounded), "
        "rolls with `coord release propagate`, and ALWAYS restarts the "
        "queue timer before exiting — whether or not anything rolled."
    ),
)
@_CONFIG_OPTION
@click.option("--target", default=None,
              help="Version to propagate (leading 'v' optional). Default: PyPI's latest.")
@click.option("--daemon-host", "daemon_host_override", default=None,
              help="Machine name running coord-serve. Normally DERIVED from the "
                   "fleet's own /health, same as `coord release propagate`.")
@click.option("--queue-timer", default="coord-drive-queue.timer", show_default=True,
              help="The systemd --user timer to stop for the duration of the drain.")
@click.option("--drain-deadline", default=3600.0, show_default=True, type=float,
              help="Bounded wait (seconds) for in-flight drives before giving up, "
                   "restarting the queue and reporting failure (trap 2) — the "
                   "queue is never left stopped past this.")
@click.option("--poll-interval", default=30.0, show_default=True, type=float,
              help="Seconds between drain re-checks.")
@click.option("--dry-run", is_flag=True,
              help="Print what this run would do; stop nothing, roll nothing.")
@click.option("--ensure-queue-running", is_flag=True,
              help="Do ONLY `systemctl --user start <queue-timer>` and exit — the "
                   "SIGKILL-safe half of trap 4/acceptance 4. Wired as "
                   "deploy/coord-release-window.service's ExecStopPost=, which "
                   "systemd runs after the main process exits for ANY reason "
                   "(success, failure, or a signal a Python `finally` cannot "
                   "catch). Every other option is ignored with this flag.")
@click.option("--json", "as_json", is_flag=True, help="Emit the window record as JSON.")
def release_nightly_window(  # noqa: PLR0912, PLR0915 — a pipeline; see module docstring
    config_path: Path,
    target: str | None,
    daemon_host_override: str | None,
    queue_timer: str,
    drain_deadline: float,
    poll_interval: float,
    dry_run: bool,
    ensure_queue_running: bool,
    as_json: bool,
) -> None:
    """One nightly-window attempt. Exit 0 on up-to-date/rolled/dry-run, 1+ otherwise.

    #2112: `coord release propagate` cannot roll the daemon host past a busy
    fleet on its own — the daemon leads every roll (the documented 405), and
    dellserver's own drive-queue tick charges itself as busy for essentially
    any queued drive (see `coord/release_window.py`'s module docstring). This
    command manufactures the window propagate can't: stop the queue, drain
    what's already running (bounded — trap 2), roll, restart the queue —
    ALWAYS restart the queue (trap 4), whatever happened in between.

    Never carries `--force` to the `coord release propagate` it shells out
    to (trap 1) — a drain that did not finish is reported and declined, not
    overridden.
    """
    import json as _json  # noqa: PLC0415
    import time  # noqa: PLC0415

    from coord import release_propagate as rp  # noqa: PLC0415
    from coord import release_verify as rv  # noqa: PLC0415
    from coord import release_window as rw  # noqa: PLC0415
    from coord.commands._common import _load_config  # noqa: PLC0415

    if ensure_queue_running:
        ok, detail = _systemctl(queue_timer, "start")
        click.echo(f"{'✓' if ok else '✗'} ensure {queue_timer} running: {detail}")
        sys.exit(0 if ok else 1)

    config = _load_config(config_path)
    state_dir = _state_dir()
    record = rw.WindowRecord(
        started_at=time.time(), dry_run=dry_run, queue_timer=queue_timer
    )

    def _finish(status: str, exit_code: int = 0) -> None:
        record.status = status
        record.finished_at = time.time()
        if not dry_run:
            try:
                rw.append_record(state_dir, record)
                rw.trim_journal(state_dir)
            except OSError as exc:
                click.echo(f"warning: could not append the window journal: {exc}",
                           err=True)
        if as_json:
            click.echo(_json.dumps(record.to_dict(), indent=2, sort_keys=True))
        else:
            click.echo("\n".join(rw.render_record(record)))
        sys.exit(exit_code)

    # ── 1. what version, and who leads? ───────────────────────────────────
    index_url = getattr(getattr(config, "health", None), "pypi_index_url",
                        "https://pypi.org/simple")
    resolved, warning = _resolve_expected(
        target, use_pypi=not target, index_url=index_url, timeout=10.0
    )
    if warning:
        click.echo(f"warning: {warning}", err=True)
    record.target_version = rp.normalize_version(resolved)
    if not record.target_version:
        record.error = (
            "could not resolve a target version — pass --target, or fix "
            "access to the PyPI simple index"
        )
        click.echo(f"error: {record.error}", err=True)
        _escalate_window(record, reason=record.error)
        _finish(rw.STATUS_ERROR, 1)

    machine_health, unreachable, daemon_facts, daemon_label = rv.gather(config, timeout=10.0)
    daemon_name = _daemon_machine_name(config, daemon_host_override, machine_health)
    if daemon_name is None:
        record.error = (
            "could not identify which machine runs coord-serve — pass "
            "--daemon-host, or fix its /health so the unit is visible "
            "(same requirement as `coord release propagate`)"
        )
        click.echo(f"error: {record.error}", err=True)
        _escalate_window(record, reason=record.error)
        _finish(rw.STATUS_ERROR, 1)
    record.daemon_host = daemon_name

    report = rv.verify(
        machine_health=machine_health, unreachable=unreachable,
        daemon_host=daemon_facts, daemon_host_name=daemon_label,
        expected=record.target_version,
    )
    record.daemon_version = _python_lane_versions(
        report, [daemon_name], record.target_version
    ).get(daemon_name)

    # ── 2. acceptance 3 — already current, so the queue is never touched ──
    if not rw.needs_roll(record.daemon_version, record.target_version):
        _finish(rw.STATUS_UP_TO_DATE, 0)

    if dry_run:
        click.echo(
            f"would stop {queue_timer}, drain up to {drain_deadline:.0f}s "
            f"({daemon_name} currently reports v{record.daemon_version or '?'}, "
            f"target v{record.target_version}), then `coord release propagate "
            f"--daemon-host {daemon_name} --target {record.target_version}`, "
            f"then restart {queue_timer}"
        )
        _finish(rw.STATUS_DRY_RUN, 0)

    # ── 3. stop the queue — no new drives may launch for the rest of this
    #      run — and from here on ALWAYS restart it before exiting ─────────
    stop_ok, stop_detail = _systemctl(queue_timer, "stop")
    record.queue_stopped = stop_ok
    record.queue_stop_detail = stop_detail
    click.echo(f"{'✓' if stop_ok else '✗'} stop {queue_timer}: {stop_detail}")

    status = rw.STATUS_ERROR
    exit_code = 1
    try:
        if not stop_ok:
            record.error = (
                f"could not stop {queue_timer} — refusing to drain or roll "
                f"without a guaranteed no-new-launches window: {stop_detail}"
            )
            click.echo(f"error: {record.error}", err=True)
            _escalate_window(record, reason=record.error)
        else:
            # ── 4. bounded drain (trap 2) ──────────────────────────────────
            outcome = _drain(
                daemon_host=daemon_name, config_path=config_path,
                deadline=drain_deadline, poll_interval=poll_interval,
                config=config,
            )
            record.drained = outcome.drained
            record.drain_seconds = outcome.elapsed_seconds
            record.drain_detail = outcome.detail
            click.echo(
                f"{'✓' if outcome.drained else '✗'} drain: "
                f"{'clean' if outcome.drained else 'TIMED OUT'} after "
                f"{outcome.elapsed_seconds:.0f}s"
                + (f" — {outcome.detail}" if outcome.detail else "")
            )

            if not outcome.drained:
                record.error = (
                    f"drain deadline ({drain_deadline:.0f}s) hit with "
                    f"{daemon_name} still busy — {outcome.detail}; declining "
                    "to roll (never --force from an unattended window)"
                )
                click.echo(f"error: {record.error}", err=True)
                _escalate_window(record, reason=record.error)
                status = rw.STATUS_DRAIN_TIMEOUT
            else:
                # ── 5. roll — the daemon host is now provably free ─────────
                prop_status, prop_exit, prop_output, prop_started_at = _run_propagate(
                    daemon_host=daemon_name, target_version=record.target_version,
                    config_path=config_path, state_dir=state_dir,
                )
                record.propagate_status = prop_status
                record.propagate_exit_code = prop_exit
                record.propagate_output = prop_output
                record.propagate_started_at = prop_started_at
                if prop_output:
                    click.echo(prop_output)

                if prop_exit == 0 and prop_status in (
                    rp.STATUS_VERIFIED, rp.STATUS_UP_TO_DATE, rp.STATUS_ROLLED,
                ):
                    status = (
                        rw.STATUS_UP_TO_DATE if prop_status == rp.STATUS_UP_TO_DATE
                        else rw.STATUS_ROLLED
                    )
                    exit_code = 0
                elif prop_exit == 0 and prop_status == rp.STATUS_DEFERRED:
                    # A drained daemon host still deferred: some OTHER host
                    # (or an unattributable signal) is busy. Not this
                    # command's #2110-shaped deadlock — that would show up
                    # as the drain never clearing — but still a night that
                    # did not roll, and just as loud (trap 3).
                    record.error = (
                        f"{daemon_name} drained clean but `coord release "
                        f"propagate` still deferred (status={prop_status}) — "
                        "see propagate_output for why"
                    )
                    click.echo(f"error: {record.error}", err=True)
                    _escalate_window(record, reason=record.error)
                    status = rw.STATUS_PROPAGATE_DEFERRED
                elif prop_status.startswith("exit ") or prop_status.startswith("error:"):
                    # #2187 proposal 3: this arm is reached only when NEITHER
                    # ground truth was available — no matching entry in
                    # `coord release history` (see
                    # `_latest_propagate_record_since`) AND no parseable
                    # `--json` payload on stdout (`_parse_trailing_json`).
                    # `prop_status` here is one of those two functions'
                    # placeholder fallbacks, never a real outcome — so the
                    # message must say exactly what evidence is missing
                    # instead of quoting the placeholder as if it were one
                    # (the old text, "status=exit 0, exit=0", read as a
                    # verification failure when exit 0 is what a VERIFIED
                    # roll also produces).
                    record.error = (
                        f"coord release propagate exited {prop_exit}, but its "
                        "outcome could not be confirmed: no matching entry "
                        "was found in `coord release history` for this run, "
                        "and its --json stdout had no parseable status "
                        "either — see propagate_output"
                    )
                    click.echo(f"error: {record.error}", err=True)
                    _escalate_window(record, reason=record.error)
                    status = rw.STATUS_PROPAGATE_FAILED
                    exit_code = prop_exit or 1
                else:
                    # A real, ground-truth status from `coord release
                    # history` — just not a verified roll (e.g. `failed` or
                    # `rolled-back`).
                    record.error = (
                        f"coord release propagate did not verify a roll — "
                        f"`coord release history` recorded status="
                        f"{prop_status!r} (exit {prop_exit}) for this run"
                    )
                    click.echo(f"error: {record.error}", err=True)
                    _escalate_window(record, reason=record.error)
                    status = rw.STATUS_PROPAGATE_FAILED
                    exit_code = prop_exit or 1
    finally:
        # Trap 4 / acceptance 4: ALWAYS restart the timer — whatever
        # happened above, including an exception raised out of this block.
        # This is the in-process half of the guarantee; --ensure-queue-running
        # wired as ExecStopPost= (deploy/coord-release-window.service) is the
        # SIGKILL-safe half, since a `finally` cannot run after SIGKILL.
        # Leaving the fleet's work queue stopped is the worst outcome this
        # mechanism exists to prevent, so this runs no matter what.
        start_ok, start_detail = _systemctl(queue_timer, "start")
        record.queue_restarted = start_ok
        record.queue_restart_detail = start_detail
        click.echo(f"{'✓' if start_ok else '✗'} restart {queue_timer}: {start_detail}")
        if not start_ok:
            _escalate_window(
                record,
                reason=(
                    "queue timer restart FAILED after a nightly window run — "
                    f"{queue_timer} may be stopped: {start_detail}. Run "
                    f"`systemctl --user start {queue_timer}` by hand NOW."
                ),
            )

    _finish(status, exit_code)


@release_group.command(
    "window-history",
    help="What the nightly release window actually did, and when (#2112).",
)
@click.option("--limit", default=40, show_default=True,
              help="Show at most this many recorded attempts (most recent last).")
@click.option("--json", "as_json", is_flag=True, help="Emit the raw records as JSON.")
def release_window_history(limit: int, as_json: bool) -> None:
    """Read the `coord release nightly-window` journal.

    Separate from `coord release history` (`release_propagate`'s journal):
    this record carries fields — the queue stop/drain/restart outcome — a
    plain propagate attempt does not have.
    """
    import json as _json  # noqa: PLC0415

    from coord import release_window as rw  # noqa: PLC0415

    records = rw.read_records(_state_dir(), limit=limit)
    if as_json:
        click.echo(_json.dumps(records, indent=2, sort_keys=True))
        return
    if not records:
        click.echo("no nightly-window attempts recorded yet")
        return
    for rec in records:
        click.echo("\n".join(rw.render_record(rec)))
        click.echo("")


# Same callback under the group, so `coord release preflight` and `coord
# release verify` are one discoverable pair. The flat `coord
# release-preflight` above keeps working unchanged.
release_group.add_command(release_preflight, name="preflight")
