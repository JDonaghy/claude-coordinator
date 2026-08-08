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


# Same callback under the group, so `coord release preflight` and `coord
# release verify` are one discoverable pair. The flat `coord
# release-preflight` above keeps working unchanged.
release_group.add_command(release_preflight, name="preflight")
