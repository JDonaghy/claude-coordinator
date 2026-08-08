"""claude-coordinator: multi-agent coordinator for Claude Code workers."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

# #1238: single-sourced from the installed package's metadata, which
# setuptools-scm stamps from the git tag at build time (see
# `[tool.setuptools_scm]` in pyproject.toml) — this is the ONLY place
# `__version__` is computed. No other source file may hardcode a version
# literal; a release is just `git tag vX.Y.Z && git push origin vX.Y.Z`.
try:
    __version__ = _pkg_version("claude-coordinator")
except PackageNotFoundError:
    # Not an installed package at all (e.g. `python -c "import coord"` run
    # directly against a source checkout that was never `pip install`'d) —
    # degrade to an obviously-not-a-release string rather than raising.
    __version__ = "0+unknown"
