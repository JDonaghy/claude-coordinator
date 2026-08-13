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

#2170 -- WHY EVERY SUBPROCESS HERE GETS A SEEDED, EMPTY ``$HOME``.

``test_coord_config_runs_without_posix_modules`` used to inherit the ambient
environment, and passed *only* on a machine whose ``$HOME`` happened to be
empty of coord state -- i.e. in CI. On a real fleet machine it failed, because
``coord`` is a **thin client** there: ``~/.coord/client.toml`` (or
``$COORD_SERVICE_URL``) names a board daemon, and once one is configured
``coord config --config <file>`` does *not* read the file you handed it. It
re-fetches ``GET /config`` from the daemon into
``~/.coord/coordinator.remote.yml`` and parses that instead. That is
documented product behaviour, not a bug -- CLAUDE.md ("On a thin client, that
resolved path is a CACHE, not the config") and ``docs/EPHEMERAL_WORKERS.md``
("``coord config --config`` is not a validator on a thin client"). So the old
test asserted the opposite of the contract and got away with it only where the
contract couldn't bite.

This is the **inverse** of the familiar "passes locally, fails only in CI ⇒
ambient ``$HOME``" pattern: same class, flipped direction, which is why it went
unnoticed for so long -- CI can never see it. The fix is therefore not just
"fix that one test": :func:`_hermetic_env` is the default for every subprocess
in this file, so no future check here can accidentally start depending on the
operator's real coord state either.
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


#: Environment variables that can point `coord` at a board daemon (see
#: `coord.client.resolve_board_service`'s bootstrap contract: flag > env >
#: `~/.coord/client.toml`). Removed from every subprocess env here.
_THIN_CLIENT_ENV = ("COORD_SERVICE_URL", "COORD_TOKEN")

#: Environment variables that can redirect config-path resolution
#: (`coord.config.resolve_config_path`: `$COORD_CONFIG` → `~/.coord/
#: coordinator.yml` → `./coordinator.yml`). Removed too, so a test that
#: passes `--config` is the only thing choosing the file.
_CONFIG_ENV = ("COORD_CONFIG",)


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, monkeypatch):
    """Point every coord/XDG state pointer at a fresh, empty ``$HOME`` and drop
    the thin-client vars, for every test in this file (#2170).

    Applied to ``os.environ`` (via ``monkeypatch``, so it unwinds) rather than
    built as a one-off dict per call site, because :func:`_run` inherits the
    ambient environment -- making the isolation the *default* is the point.
    ``PATH``/``VIRTUAL_ENV``/``PYTHONPATH`` are deliberately untouched: the
    subprocess must still be able to import the ``coord`` package under test.
    What is neutralised is only the *state discovery* an ambient ``$HOME``
    supplies -- ``~/.coord/client.toml``, ``~/.coord/coordinator.yml``,
    ``~/.coord/coordinator.remote.yml``.

    ``coord.client.COORD_DIR`` is ``Path.home() / ".coord"`` evaluated at
    *import* time, which is why redirecting ``$HOME`` works at all here: each
    check runs in a fresh interpreter (see the module docstring) that resolves
    it against the value set below.

    ``USERPROFILE`` is set alongside ``HOME`` because ``Path.home()`` consults
    it on Windows -- this file is the cross-platform-portability suite, so its
    own isolation had better not be POSIX-only.
    """
    home = tmp_path / "hermetic-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for xdg in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(xdg, str(home / xdg.lower()))
    for var in (*_THIN_CLIENT_ENV, *_CONFIG_ENV):
        monkeypatch.delenv(var, raising=False)
    return home


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


#: The fixture config the ``coord config`` checks below hand to ``--config``.
#: Deliberately nothing like the real fleet's, so "did it read MY file or the
#: daemon's?" is answerable from the printed output alone.
_FIXTURE_CONFIG = """
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


def test_coord_config_runs_without_posix_modules(tmp_path) -> None:
    """``coord config`` -- read/plan surface, parses+prints a real coordinator.yml.

    Reads *its own fixture* only because the autouse ``_hermetic_home`` fixture
    neutralises the thin-client re-fetch path (#2170). Without that, on any
    machine with a board daemon configured, this prints the **fleet's** repos
    and machines and fails -- see the module docstring, and see
    :func:`test_coord_config_on_a_thin_client_does_not_read_the_given_file`
    for the same command with that path switched back on.
    """
    cfg_path = tmp_path / "coordinator.yml"
    cfg_path.write_text(_FIXTURE_CONFIG)
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
    # The resolved-path banner is the direct proof of WHICH file was parsed --
    # `coord config` prints it precisely so this is never ambiguous (CLAUDE.md).
    assert f"# {cfg_path}" in result.stdout
    assert "Repos:" in result.stdout
    assert "api (acme/api)" in result.stdout
    assert "Machines:" in result.stdout
    assert "laptop @ laptop.tailnet" in result.stdout


def test_coord_config_on_a_thin_client_does_not_read_the_given_file(
    tmp_path, monkeypatch
) -> None:
    """The contract that made the test above environment-dependent, pinned (#2170).

    Same command, same fixture, one difference: ``$COORD_SERVICE_URL`` names a
    board daemon. `coord` is then a thin client, and ``--config`` is **not** a
    validator -- ``_load_config`` re-fetches ``GET /config`` and parses the
    daemon's answer instead of the path it was handed
    (``docs/EPHEMERAL_WORKERS.md``, CLAUDE.md).

    Pointing that at an *unreachable* daemon makes the branch observable
    without needing a live one: #1080's rule is that a thin client must never
    fall through to a local file that happens to exist, so this exits 2 saying
    it could not fetch -- and, crucially, never prints the fixture's contents.
    That non-print is the assertion that matters. It is what the sibling test
    above would have hit on `precision` (where the daemon *is* reachable, so
    the fleet's real repos got printed instead), and it is why that test has to
    neutralise this path rather than assume the environment is empty.

    Guards the fix from the obvious wrong turn, too: "make ``--config``
    authoritative" would make this test fail, which is the signal that it would
    have been a product regression, not a test fix.
    """
    cfg_path = tmp_path / "coordinator.yml"
    cfg_path.write_text(_FIXTURE_CONFIG)
    # Port 9 is the IANA discard port and is not listened on; a loopback
    # connect fails immediately with ECONNREFUSED rather than waiting out
    # httpx's 5s timeout, so this stays a fast test with no network egress.
    monkeypatch.setenv("COORD_SERVICE_URL", "http://127.0.0.1:9")
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
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert "could not fetch config from http://127.0.0.1:9" in result.stderr
    # The whole point: the file it was handed was never parsed or printed.
    assert "api (acme/api)" not in result.stdout
    assert "laptop @ laptop.tailnet" not in result.stdout
