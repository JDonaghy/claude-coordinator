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
    from coord.health.pypi import latest_release  # noqa: PLC0415

    try:
        latest, _all = latest_release(
            "claude-coordinator", index_url=index_url, timeout=timeout
        )
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
    default=False,
    help="Resolve --expected from the PyPI simple index (the released version).",
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


def _daemon_machine_name(config, override: str | None) -> str | None:
    """Which machine in ``coordinator.yml`` runs ``coord-serve``.

    The daemon must lead every roll (see :func:`coord.release_propagate.
    plan_lanes`), so getting this wrong is not cosmetic — it reintroduces
    the documented 405. Resolution order: the explicit flag, then the host
    in the configured ``board_service`` URL matched against each machine's
    host, then None (in which case ordering degrades to config order and
    the command says so out loud rather than pretending).
    """
    machines = list(getattr(config, "machines", ()) or ())
    if override:
        return override
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
        "Roll the released version onto the fleet at the next QUIESCENT "
        "window, verify it, and roll back on red (#1835). Safe to run from "
        "a timer: a busy fleet is a recorded deferral, not a failure."
    ),
)
@_CONFIG_OPTION
@click.option("--target", "target", default=None,
              help="Version to propagate (leading 'v' optional). Default: PyPI's latest.")
@click.option("--daemon-host", "daemon_host_override", default=None,
              help="Machine name running coord-serve. It rolls FIRST — a caller "
                   "must never reach an endpoint its daemon predates.")
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
                   "when verification comes back CRIT.")
@click.option("--release-holds/--no-release-holds", "release_holds", default=True,
              show_default=True,
              help="After a VERIFIED roll, release the drive-queue deploy gates "
                   "(#1757) that were waiting for exactly this deploy.")
@click.option("--timeout", default=180.0, show_default=True,
              help="Seconds to wait for each agent to report the new version.")
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
    as_json: bool,
) -> None:
    """One propagation attempt. Exit 0 on deferral, 1 on red, 2 on rollback."""
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

    # ── 2. is there a window? ────────────────────────────────────────────
    board, board_error = _fetch_board()
    extra_busy = []
    if board_error:
        extra_busy.append(
            rp.Busy(kind="board unreadable", subject="/board", detail=board_error)
        )
    quiescence = rp.assess_quiescence(
        queue_entries=board.get("drive_queue") or [],
        assignments=board.get("assignments") or [],
        extra_busy=extra_busy,
    )
    record.quiescence = quiescence.to_dict()

    if not quiescence.quiescent and not force:
        # The single most important line in this command: a deferral is a
        # normal, recorded, exit-0 outcome. A timer that defers all night
        # must be visibly *working*, not visibly failing.
        _finish(rp.STATUS_DEFERRED, 0)
    if not quiescence.quiescent and force:
        click.echo(
            "warning: --force — rolling over a BUSY fleet; in-flight "
            f"headless workers will be killed ({quiescence.reason})",
            err=True,
        )

    # ── 3. who still needs it, and in what order? ────────────────────────
    machine_health, unreachable, daemon_facts, daemon_label = rv.gather(
        config, timeout=10.0
    )
    before = rv.verify(
        machine_health=machine_health, unreachable=unreachable,
        daemon_host=daemon_facts, daemon_host_name=daemon_label,
        expected=record.target_version,
    )
    hosts = [m.name for m in (getattr(config, "machines", ()) or ())]
    daemon_name = _daemon_machine_name(config, daemon_host_override)
    if daemon_name is None:
        click.echo(
            "warning: could not identify which machine runs coord-serve — "
            "rolling in coordinator.yml order. Pass --daemon-host to pin it; "
            "the daemon must never lag a caller (the documented 405).",
            err=True,
        )
    current = rp.hosts_already_current(_lane_versions_by_host(before), record.target_version)
    rolls = rp.plan_lanes(
        daemon_host=daemon_name,
        hosts=hosts,
        lanes=lane_filter or rp.ALL_LANES,
        skip_hosts=current,
    )
    for host in current:
        record.lanes.append(
            {"lane": "-", "host": host, "ok": None,
             "detail": f"already on v{record.target_version}"}
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
        _finish(rp.STATUS_ROLLED if rolls else rp.STATUS_UP_TO_DATE, 0)

    if not rolls:
        _finish(rp.STATUS_UP_TO_DATE, 0)

    # ── 4. roll, in the planned order ────────────────────────────────────
    by_name = {m.name: m for m in (getattr(config, "machines", ()) or ())}
    updated_hosts: list[str] = []
    local_name = _local_machine_name(config)

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

        if roll.lane == rp.LANE_PYTHON:
            ok, detail = _roll_python(
                machine, target_version=record.target_version,
                agent_port=AGENT_PORT, timeout=timeout, force=force,
            )
            if ok:
                updated_hosts.append(roll.host)
        elif roll.lane == rp.LANE_UNITS:
            ok, detail = _roll_units(machine, agent_port=AGENT_PORT)
        else:
            ok, detail = _roll_tui(
                machine, target_version=record.target_version, local_name=local_name
            )

        record.lanes.append({"lane": roll.lane, "host": roll.host, "ok": ok,
                             "detail": detail})
        click.echo(f"  {'✓' if ok else '✗'} {roll.label}: {detail}")

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

    if after.severity == "crit" and rollback_on_red:
        # #1835: "a red post-deploy verification must roll back, not just
        # report." Only the hosts THIS run updated — rolling back a host we
        # never touched would undo somebody else's deliberate state.
        for host in updated_hosts:
            machine = by_name.get(host)
            if machine is None:
                continue
            ok, detail = _rollback_host(machine, agent_port=AGENT_PORT)
            record.rolled_back.append(f"{host}: {detail}")
            click.echo(f"  {'↩' if ok else '✗'} rollback {host}: {detail}")
        _finish(rp.STATUS_ROLLED_BACK, 2)

    if after.severity == "crit":
        _finish(rp.STATUS_FAILED, 1)

    # ── 6. release the deploy gates that were waiting for this ───────────
    verified = True
    for key in rp.holds_to_release(quiescence, verified=verified):
        if not release_holds:
            click.echo(f"  · deploy gate {key} left held (--no-release-holds)")
            continue
        ok, detail = _release_hold(key)
        if ok:
            record.released_holds.append(key)
        click.echo(f"  {'✓' if ok else '✗'} release deploy gate {key}: {detail}")

    _finish(rp.STATUS_VERIFIED, 0)


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
                 force: bool) -> tuple[bool, str]:
    """POST /update and wait for the agent to actually report the version.

    Success is judged by the version the agent reports, never by "the POST
    was accepted" (#1568: a stale pip index makes a no-op look like a
    success) — the wait loop is ``coord agent update``'s own, reused rather
    than reimplemented so the two can't drift.
    """
    from coord.commands.agent_ops import (  # noqa: PLC0415
        _fetch_pre_started_at,
        _wait_agents_updated,
    )

    pre = _fetch_pre_started_at([machine])
    status, body, error = _post(
        f"http://{machine.host}:{agent_port}/update",
        {"target_version": target_version, "force": force},
        timeout=15.0,
    )
    if error:
        return False, error
    if status == 409:
        # The agent refused: live sessions, or an editable install. Both are
        # correct refusals and neither is this command's to override.
        return False, str(body.get("error") or "refused (409)")
    if status != 202:
        return False, f"HTTP {status}"

    outcomes = _wait_agents_updated(
        [machine], target_version=target_version, timeout=timeout,
        pre_started_at=pre,
    )
    outcome = outcomes.get(machine.name) or {}
    if outcome.get("matched"):
        return True, f"now v{target_version}"
    return False, str(
        outcome.get("error")
        or f"still reporting v{outcome.get('version_now', '?')} after "
           f"{timeout:.0f}s (last update result: {outcome.get('result')})"
    )


def _roll_units(machine, *, agent_port: int) -> tuple[bool, str]:
    """POST /deploy-units — the `deploy/**` lane's deploy step (#1831)."""
    status, body, error = _post(
        f"http://{machine.host}:{agent_port}/deploy-units", {}, timeout=30.0
    )
    if error:
        return False, error
    if status in (404, 405):
        # Bootstrap: this agent predates the endpoint. It will have it after
        # the python lane above lands, so this is a *next run* fact, not a
        # failure — recorded rather than swallowed.
        return False, ("agent has no /deploy-units yet (predates #1835) — "
                       "the next propagation will roll this lane")
    if status != 200:
        return False, str(body.get("error") or body.get("summary") or f"HTTP {status}")
    units = body.get("units") or []
    changed = [u.get("name") for u in units if u.get("action") == "updated"]
    new = [u.get("name") for u in units if u.get("action") == "new"]
    parts = []
    parts.append(f"{len(changed)} unit(s) refreshed" if changed else "units already current")
    if body.get("reloaded"):
        parts.append("daemon-reload ok")
    if new:
        parts.append(
            f"{len(new)} packaged unit(s) NOT installed here ({', '.join(sorted(map(str, new)))}) "
            "— a release does not decide which services a host runs"
        )
    return True, "; ".join(parts)


def _roll_tui(machine, *, target_version: str, local_name: str | None) -> tuple[bool, str]:
    """`coord tui update` — local host only, and honest about the rest.

    ``coord-tui`` is a binary in each host's ``~/.local/bin``; there is no
    agent endpoint that installs it, so this lane can only roll where this
    command is running. Remote hosts are recorded as an explicit gap rather
    than silently omitted — a lane nobody can see is the lane that bites
    (#1834).
    """
    import subprocess  # noqa: PLC0415

    if local_name is None or machine.name != local_name:
        return False, (
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


def _rollback_host(machine, *, agent_port: int) -> tuple[bool, str]:
    """POST /rollback — back to the previous blue/green generation (#1241)."""
    status, body, error = _post(
        f"http://{machine.host}:{agent_port}/rollback", {"force": True}, timeout=30.0
    )
    if error:
        return False, error
    if status == 202:
        return True, "rolling back to the previous generation"
    if status == 404:
        return False, "no previous generation on this host"
    return False, str(body.get("error") or f"HTTP {status}")


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
    "rollback",
    help=(
        "ONE command that puts every agent back on its previous venv "
        "generation (#1241/#1560). The escape hatch for a bad release."
    ),
)
@_CONFIG_OPTION
@click.option("--machine", "machine_filter", default=None, help="Only this machine.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def release_rollback(config_path: Path, machine_filter: str | None, yes: bool) -> None:
    """#1560 requires rollback to be one command, not a runbook.

    Every successful ``/update`` leaves the previous generation on disk
    (``coord.agent_update``'s two fixed blue/green slots) precisely so this
    can exist. It force-rolls: a rollback is what you reach for when the
    fleet is broken, and refusing because a worker is running on a broken
    release would be the wrong tradeoff at exactly the wrong moment.
    """
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
        ok, detail = _rollback_host(machine, agent_port=AGENT_PORT)
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


# Same callback under the group, so `coord release preflight` and `coord
# release verify` are one discoverable pair. The flat `coord
# release-preflight` above keeps working unchanged.
release_group.add_command(release_preflight, name="preflight")
