#!/usr/bin/env python3
"""Assert a built ``dist/`` is a releasable, correctly-stamped artifact set.

#1242 (PKG-6): a ``v*`` tag must produce ONE release in which every piece —
wheel, sdist, webapp bundle, coord-tui binaries — carries the same
tag-derived version.  The coord-tui half already proves this for itself
(``release-tui.yml``'s "Verify --version reports the tag version" step).
This script is the Python half, run by ``publish.yml`` between
``python -m build`` and the PyPI upload, and it checks three things that are
each *silent* failures otherwise:

1. **The version came from the tag.**  ``pyproject.toml`` has no version
   literal any more — setuptools-scm derives it from the nearest reachable
   git tag (#1238), and falls back to ``X.Y.Z.devN+g<sha>`` (or
   ``0.0.0.dev0``) rather than crashing when it can't see one.  That
   fallback is the right behaviour for a dev checkout and exactly the wrong
   thing to upload to PyPI: a shallow/tagless CI clone would publish
   ``0.4.106.dev3+gdeadbee`` under a ``v0.4.106`` release, and PyPI uploads
   are immutable.  Comparing the built filename's version against the tag
   turns that into a failed job instead.

2. **The ``[server]`` extra survived.**  PKG-1 (#1237) split the install into
   a small client base plus a ``server`` extra, and the whole
   agent/daemon fleet installs ``code-coordinator[server]``.  If that
   extra ever stops being declared in the built metadata, every
   ``install-agent.sh`` / ``coord agent update`` silently installs a
   *client* onto a server host.

3. **The webapp bundle is inside the wheel.**  ``coord/dashboard/webapp/dist``
   is gitignored and force-included via ``MANIFEST.in`` + package-data, so
   a wheel built without the preceding ``npm run build`` is perfectly valid
   and perfectly useless — ``coord web`` serves a 404 shell.

Kept as a standalone script (rather than an inline heredoc in the workflow)
so tests/test_release_unified_1242.py can drive it against synthetic dists
without a GitHub Actions runner.
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

from packaging.version import InvalidVersion, Version

#: Path inside the wheel that only exists when the React bundle was built
#: before ``python -m build`` (see MANIFEST.in / [tool.setuptools.package-data]).
WEBAPP_MARKER_PREFIX = "coord/dashboard/webapp/dist/"

#: The optional-dependency group every agent host and the daemon host must
#: install (pyproject.toml ``[project.optional-dependencies] server``).
REQUIRED_EXTRA = "server"


class VerificationError(Exception):
    """One of the release invariants above does not hold."""


def normalise_tag(tag: str) -> Version:
    """``v0.4.106`` / ``0.4.106`` / ``v1.0.0-rc1`` -> a comparable
    :class:`Version`.  PEP 440 normalisation is why this goes through
    ``packaging`` rather than a string compare: setuptools-scm writes
    ``1.0.0rc1`` into the filename for a ``v1.0.0-rc1`` tag, and those two
    spellings are the same version."""
    stripped = tag.strip()
    if stripped.startswith("v"):
        stripped = stripped[1:]
    try:
        return Version(stripped)
    except InvalidVersion as exc:  # pragma: no cover - argparse-level misuse
        raise VerificationError(f"tag {tag!r} is not a PEP 440 version: {exc}") from exc


def wheel_version(path: Path) -> Version:
    """The version segment of a wheel filename
    (``{name}-{version}-{python}-{abi}-{platform}.whl``)."""
    parts = path.name[: -len(".whl")].split("-")
    if len(parts) < 5:
        raise VerificationError(f"{path.name} is not a well-formed wheel filename")
    return Version(parts[1])


def sdist_version(path: Path) -> Version:
    """The version segment of an sdist filename (``{name}-{version}.tar.gz``)."""
    stem = path.name[: -len(".tar.gz")]
    match = re.match(r"^(?P<name>.+)-(?P<version>[^-]+)$", stem)
    if not match:
        raise VerificationError(f"{path.name} is not a well-formed sdist filename")
    return Version(match.group("version"))


def wheel_metadata(path: Path) -> str:
    """The wheel's ``*.dist-info/METADATA`` contents."""
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.endswith(".dist-info/METADATA")]
        if not names:
            raise VerificationError(f"{path.name} contains no .dist-info/METADATA")
        return zf.read(names[0]).decode("utf-8", errors="replace")


def wheel_contains_webapp(path: Path) -> bool:
    with zipfile.ZipFile(path) as zf:
        return any(n.startswith(WEBAPP_MARKER_PREFIX) and not n.endswith("/") for n in zf.namelist())


def verify(dist: Path, tag: str, *, require_webapp: bool = True) -> Version:
    """Run every check against *dist*; return the verified version.

    Collects *all* failures before raising rather than stopping at the first
    one — a release job that reports "version is wrong" and then, on the next
    run, "and the webapp is missing too" wastes a whole CI cycle per problem.
    """
    expected = normalise_tag(tag)
    problems: list[str] = []

    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))

    if len(wheels) != 1:
        problems.append(
            f"expected exactly 1 wheel in {dist}, found {len(wheels)}: "
            f"{[p.name for p in wheels]}"
        )
    if len(sdists) != 1:
        problems.append(
            f"expected exactly 1 sdist in {dist}, found {len(sdists)}: "
            f"{[p.name for p in sdists]}"
        )

    for built in (*wheels, *sdists):
        try:
            found = wheel_version(built) if built.suffix == ".whl" else sdist_version(built)
        except (VerificationError, InvalidVersion) as exc:
            problems.append(str(exc))
            continue
        if found != expected:
            problems.append(
                f"{built.name} is version {found}, but the tag is {tag} "
                f"(expected {expected}) — setuptools-scm did not resolve the "
                "tag. Is the checkout shallow / missing tags?"
            )

    for wheel in wheels:
        try:
            metadata = wheel_metadata(wheel)
        except VerificationError as exc:
            problems.append(str(exc))
        else:
            if f"Provides-Extra: {REQUIRED_EXTRA}" not in metadata:
                problems.append(
                    f"{wheel.name} declares no `Provides-Extra: {REQUIRED_EXTRA}` — "
                    "every agent/daemon host installs "
                    f"code-coordinator[{REQUIRED_EXTRA}] and would silently get "
                    "a client-only install"
                )
        if require_webapp and not wheel_contains_webapp(wheel):
            problems.append(
                f"{wheel.name} contains no {WEBAPP_MARKER_PREFIX}* files — the "
                "React bundle was not built before `python -m build`, so "
                "`coord web` would serve an empty shell"
            )

    if problems:
        raise VerificationError("\n".join(problems))
    return expected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", required=True, help="the release tag, e.g. v0.4.106")
    parser.add_argument("--dist", default="dist", type=Path, help="directory holding the built wheel/sdist")
    parser.add_argument(
        "--no-webapp",
        action="store_true",
        help="skip the bundled-React-app check (for local builds that skip `npm run build`)",
    )
    args = parser.parse_args(argv)

    try:
        version = verify(args.dist, args.tag, require_webapp=not args.no_webapp)
    except VerificationError as exc:
        for line in str(exc).splitlines():
            # `::error::` so each problem surfaces as a GitHub Actions
            # annotation rather than being buried in the step log.
            print(f"::error::{line}", file=sys.stderr)
        return 1

    print(f"release artifacts verified at version {version}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
