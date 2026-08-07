"""Windows/macOS import portability (#1156, CP-1).

The blast radius in the issue was measured with a ``sys.meta_path`` finder
that raises ``ImportError`` for the POSIX-only stdlib modules Windows lacks
(``fcntl``/``termios``/``tty``/``pty``/``pwd``/``grp``/``resource``), then
importing each entry point.  These tests reproduce that harness so a
regression (someone adds a top-level ``import fcntl`` back to a hot path)
fails CI on Linux -- no Windows box required.

Each check runs in a **subprocess**.  A same-process ``sys.meta_path``
install would be too late: pytest collection has already imported
``coord.cli``/``coord.drive``/``coord.dashboard.server`` (directly or
transitively) by the time any test body runs, so the blocked names would
already be sitting in ``sys.modules`` and the finder would never be
consulted. A fresh interpreter is the only way to prove the import order
the issue actually cares about.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

#: Same list as the issue's own blast-radius harness.
_BLOCKED_POSIX_MODULES = ("fcntl", "termios", "tty", "pty", "pwd", "grp", "resource")

_BLOCKER_PREAMBLE = f"""
import sys, importlib.abc

_BLOCKED = {_BLOCKED_POSIX_MODULES!r}

class _BlockPosix(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name in _BLOCKED:
            raise ImportError(f"{{name}} blocked (simulated non-POSIX)")
        return None

sys.meta_path.insert(0, _BlockPosix())
"""


def _run(script: str, *, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


@pytest.mark.parametrize(
    "module",
    ["coord.cli", "coord.drive", "coord.dashboard.server"],
)
def test_entry_point_imports_without_posix_modules(module: str) -> None:
    """The three modules the issue found dead on Windows now import clean."""
    result = _run(_BLOCKER_PREAMBLE + f"\nimport {module}\nprint('OK')\n")
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


@pytest.mark.parametrize(
    "module",
    [
        "coord",
        "coord.config",
        "coord.state",
        "coord.agent",
        "coord.agent_app",
        "coord.serve_app",
        "coord.dispatch",
        "coord.drive_queue",
        "coord.review",
        "coord.filelock",
        "coord.interactive",
        "coord.dashboard.terminal",
    ],
)
def test_other_surfaces_import_without_posix_modules(module: str) -> None:
    """Every module the issue's decision touched, plus the ones already clean."""
    result = _run(_BLOCKER_PREAMBLE + f"\nimport {module}\nprint('OK')\n")
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_coord_help_runs_without_posix_modules() -> None:
    """``coord --help`` -- the whole CLI used to die on `import fcntl` (#1156)."""
    script = _BLOCKER_PREAMBLE + """
import sys
sys.argv = ["coord", "--help"]
from coord.cli import main
try:
    main()
except SystemExit as exc:
    sys.exit(exc.code or 0)
"""
    result = _run(script)
    assert result.returncode == 0, result.stderr
    assert "Usage: coord" in result.stdout
    assert "status" in result.stdout


def test_coord_config_runs_without_posix_modules(tmp_path) -> None:
    """``coord config`` -- read/plan surface, parses+prints a real coordinator.yml."""
    cfg_path = tmp_path / "coordinator.yml"
    cfg_path.write_text(
        """
repos:
  - name: api
    github: acme/api
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /home/user/src/api
"""
    )
    script = _BLOCKER_PREAMBLE + f"""
import sys
sys.argv = ["coord", "config", "--config", {str(cfg_path)!r}]
from coord.cli import main
try:
    main()
except SystemExit as exc:
    sys.exit(exc.code or 0)
"""
    result = _run(script)
    assert result.returncode == 0, result.stderr
    assert "Repos:" in result.stdout
    assert "api (acme/api)" in result.stdout
    assert "Machines:" in result.stdout
    assert "laptop @ laptop.tailnet" in result.stdout
