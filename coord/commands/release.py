"""`coord release-preflight` — local sanity checks before cutting a release.

#1471: `main` is now a protected branch, so a plain ``git push origin main``
can be silently *rejected* while a subsequent ``git push origin vX.Y.Z``
still *succeeds* — the two pushes are independent refs and nothing couples
them. That let a v0.4.82 release publish (immutably) to PyPI from a commit
that, at that moment, existed nowhere but the releaser's local checkout and
the tag.

This command is a fast, local, no-side-effects check meant to run right
before tagging a release, per the fixed flow in docs/AGENT_OPERATIONS.md
(bump -> branch -> PR -> merge -> pull merged main -> tag -> push tag). It
does not push, tag, or modify anything itself.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import click


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _read_pyproject_version(repo_root: Path) -> str | None:
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.exists():
        return None
    try:
        data = tomllib.loads(pyproject.read_text())
    except Exception:  # noqa: BLE001 — malformed file surfaces as "could not read"
        return None
    return data.get("project", {}).get("version")


def _read_init_version(repo_root: Path) -> str | None:
    init_path = repo_root / "coord" / "__init__.py"
    if not init_path.exists():
        return None
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', init_path.read_text())
    return match.group(1) if match else None


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
    - ``pyproject.toml``'s ``version`` and ``coord/__init__.py``'s
      ``__version__`` agree
    - that version isn't already tagged (guards against re-tagging or a
      forgotten bump)
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
        problems.append(f"not on main (currently on '{branch}') — check out main before releasing")

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
                "version bump must land there via a merged PR *before* you tag "
                "it (#1471) — a tag built from a commit main rejected still "
                "publishes to PyPI, and PyPI releases are immutable."
            )

    pyproject_version = _read_pyproject_version(repo_root)
    init_version = _read_init_version(repo_root)
    if pyproject_version is None:
        problems.append("could not read version from pyproject.toml")
    if init_version is None:
        problems.append("could not read __version__ from coord/__init__.py")
    if pyproject_version and init_version and pyproject_version != init_version:
        problems.append(
            f"version mismatch: pyproject.toml={pyproject_version} vs "
            f"coord/__init__.py={init_version} — these must match"
        )

    if pyproject_version:
        tag = f"v{pyproject_version}"
        tag_check = _git(repo_root, "rev-parse", "-q", "--verify", f"refs/tags/{tag}")
        if tag_check.returncode == 0:
            problems.append(f"tag {tag} already exists — bump the version before releasing")

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
    fetches ``origin/main`` and confirms local ``main`` matches it, the
    working tree is clean, and the version bump is consistent — so the
    #1471 failure mode (tagging a commit that never actually landed on the
    protected ``main`` branch) is caught locally instead of shipping an
    immutable bad PyPI release.
    """
    repo_root = Path(path_opt).expanduser() if path_opt else Path.cwd()
    problems = release_preflight_checks(repo_root)
    if problems:
        click.echo("release preflight FAILED:", err=True)
        for problem in problems:
            click.echo(f"  - {problem}", err=True)
        sys.exit(1)
    version = _read_pyproject_version(repo_root)
    click.echo(
        f"release preflight OK — local main matches origin/main, working "
        f"tree clean, version {version} ready to tag."
    )
