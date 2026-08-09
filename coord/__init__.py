"""claude-coordinator: multi-agent coordinator for Claude Code workers."""

from __future__ import annotations

from importlib.metadata import Distribution, PackageNotFoundError
from importlib.metadata import version as _pkg_version


def _editable_source_root(dist_name: str):
    """Return the source checkout root when *dist_name* is installed
    editable (PEP 660, ``pip install -e .``), else ``None``.

    #2010: an editable install's ``.dist-info`` is written once at install
    time and never refreshed — reading ``__version__`` straight off it goes
    stale the moment ``git pull`` moves the checkout's HEAD past whatever
    tag was current at install time, misreporting the *operator's own* CLI
    as drifted rather than the fleet it's inspecting.

    pip records editable installs via a ``direct_url.json`` with
    ``dir_info.editable: true`` and a ``file://`` URL pointing at the live
    source tree — that's the reliable signal, not a ``site-packages``
    substring match on ``__file__`` (a non-editable install into a venv
    satisfies that too, and its metadata IS trustworthy since it's a
    frozen snapshot rather than a claim about live source).
    """
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415
    from urllib.parse import unquote, urlparse  # noqa: PLC0415

    try:
        raw = Distribution.from_name(dist_name).read_text("direct_url.json")
        info = json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001 — best-effort, never break __version__
        return None
    if not isinstance(info, dict) or not info.get("dir_info", {}).get("editable"):
        return None
    parsed = urlparse(info.get("url", ""))
    if parsed.scheme != "file" or not parsed.path:
        return None
    root = Path(unquote(parsed.path))
    return root if root.is_dir() else None


def _live_scm_version(root) -> str | None:
    """Best-effort live version for an editable checkout at *root*, so
    ``__version__`` doesn't trust a ``.dist-info`` stamp frozen at ``pip
    install -e .`` time (#2010). Two tiers, most-accurate first:

    1. ``setuptools_scm.get_version()`` — the exact scheme a wheel build
       would stamp from the same commit. Only importable when the
       environment kept the build-system requirement around (e.g. ``pip
       install -e ".[dev]"``); a plain ``pip install -e .`` discards it
       once PEP 517 build isolation tears down its throwaway venv.
    2. ``git describe --tags --dirty --always`` — always available
       wherever git is, and identical to the wheel's version string on a
       clean tagged commit (the common case); slightly less precise
       (raw git-describe form, not PEP 440) when HEAD has moved past the
       last tag, but still honest and never a stale number.

    Returns ``None`` — never raises — when neither works; callers fall
    back to the (possibly stale) package metadata.
    """
    try:
        from setuptools_scm import get_version  # noqa: PLC0415

        return get_version(root=str(root), fallback_version="0.0.0.dev0")
    except Exception:  # noqa: BLE001 — setuptools-scm may not be importable
        pass

    import subprocess  # noqa: PLC0415

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "describe", "--tags", "--dirty", "--always"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        described = result.stdout.strip()
        if not described:
            return None
        return described[1:] if described.startswith("v") else described
    except Exception:  # noqa: BLE001 — best-effort, never break __version__
        return None


def _resolve_version(dist_name: str) -> str:
    """#1238: single-sourced from the installed package's metadata, which
    setuptools-scm stamps from the git tag at build time (see
    ``[tool.setuptools_scm]`` in pyproject.toml) — this is the ONLY place
    ``__version__`` is computed. No other source file may hardcode a
    version literal; a release is just ``git tag vX.Y.Z && git push origin
    vX.Y.Z``.

    #2010: for a wheel install that metadata is always correct — it's a
    frozen snapshot of a build that just happened. For an *editable*
    install it is a snapshot written once at ``pip install -e .`` time and
    never refreshed; ``git pull`` can move the checkout well past it with
    nothing updating ``.dist-info``, so the operator's own CLI ends up
    reporting itself as ancient. When the install is editable, prefer a
    live git-derived version over the frozen metadata so ``coord
    --version`` (and the ``coord status`` drift check that compares agent
    versions against it) reflects the code actually running, rather than
    accusing every agent of drift from a number that was wrong about the
    operator, not them.
    """
    try:
        installed_version = _pkg_version(dist_name)
    except PackageNotFoundError:
        # Not an installed package at all (e.g. `python -c "import coord"`
        # run directly against a source checkout that was never
        # `pip install`'d) — degrade to an obviously-not-a-release string
        # rather than raising.
        return "0+unknown"

    root = _editable_source_root(dist_name)
    if root is not None:
        scm_version = _live_scm_version(root)
        if scm_version is not None:
            return scm_version
    return installed_version


__version__ = _resolve_version("claude-coordinator")
