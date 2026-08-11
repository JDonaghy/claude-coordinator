"""Tests for coord.dist_name (#2103): resolve either distribution name
*before* the `claude-coordinator` -> `code-coordinator` rename (epic #2096)
lands, so a hardcoded `importlib.metadata` lookup doesn't go stale the
moment some machine in the fleet has the new name installed instead.

``TestResolveInstalled`` / ``TestResolveInstalledName`` / ``TestPkgSpec``
are fast unit tests, one per #2103 acceptance criterion (1-4), mocking only
``coord.dist_name._pkg_version`` (the one `importlib.metadata.version` call
this module makes) rather than the five call sites that use this module.

``TestBuildUnderNewName`` is acceptance #2 verbatim: build a real wheel
under the *new* name and install it, then prove ``resolve_installed()``
finds it for real — no ``importlib.metadata`` mocking anywhere in that
class. Reuses the wheel-build harness from
``tests/test_version_single_source.py`` (#1238's same "build for real,
don't mock the build backend" approach).
"""

from __future__ import annotations

import os
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from unittest.mock import patch

import pytest

from coord.dist_name import (
    CANDIDATE_NAMES,
    DistributionNotFoundError,
    ResolvedDist,
    pkg_spec,
    resolve_installed,
    resolve_installed_name,
)
from tests.test_version_single_source import REPO_ROOT, _build_wheel, _run


def _fake_pkg_version(available: dict):
    """A stand-in for ``importlib.metadata.version`` that only knows about
    the names in *available* — everything else raises
    ``PackageNotFoundError``, exactly like the real thing does for an
    uninstalled distribution."""

    def _version(name: str) -> str:
        try:
            return available[name]
        except KeyError:
            raise PackageNotFoundError(name) from None

    return _version


class TestResolveInstalled:
    def test_preference_order_is_new_name_first(self) -> None:
        """Pinned so a reordering (accidental or not) is caught here rather
        than only showing up as "both installed" behavior changing."""
        assert CANDIDATE_NAMES == ("code-coordinator", "claude-coordinator")

    def test_resolves_claude_coordinator_when_only_that_is_installed(self) -> None:
        """#2103 acceptance #1: with only `claude-coordinator` installed,
        resolution matches pre-fix behavior exactly."""
        with patch(
            "coord.dist_name._pkg_version",
            side_effect=_fake_pkg_version({"claude-coordinator": "1.2.3"}),
        ):
            assert resolve_installed() == ResolvedDist(name="claude-coordinator", version="1.2.3")

    def test_resolves_code_coordinator_when_only_that_is_installed(self) -> None:
        """#2103 acceptance #2 (unit half — see TestBuildUnderNewName below
        for the real-wheel black-box version of this same criterion)."""
        with patch(
            "coord.dist_name._pkg_version",
            side_effect=_fake_pkg_version({"code-coordinator": "4.5.6"}),
        ):
            assert resolve_installed() == ResolvedDist(name="code-coordinator", version="4.5.6")

    def test_prefers_code_coordinator_when_both_installed(self) -> None:
        """#2103 acceptance #3: the transient cutover state — the new name
        wins, and the *readout says so*: callers get a name back, not just
        a version string that hides which distribution it came from."""
        with patch(
            "coord.dist_name._pkg_version",
            side_effect=_fake_pkg_version(
                {"code-coordinator": "4.5.6", "claude-coordinator": "1.2.3"}
            ),
        ):
            resolved = resolve_installed()
        assert resolved.name == "code-coordinator"
        assert resolved.version == "4.5.6"

    def test_raises_naming_both_when_neither_installed(self) -> None:
        """#2103 acceptance #4: never a bare `None` — the failure is
        explicit and names every candidate tried."""
        with patch("coord.dist_name._pkg_version", side_effect=_fake_pkg_version({})):
            with pytest.raises(DistributionNotFoundError) as exc_info:
                resolve_installed()
        message = str(exc_info.value)
        assert "code-coordinator" in message
        assert "claude-coordinator" in message


class TestResolveInstalledName:
    """The tolerant-`None`-on-miss wrapper used by best-effort reporting
    sites (`_detect_install_mode`, the CLI's stale-install hint)."""

    def test_returns_the_resolved_name(self) -> None:
        with patch(
            "coord.dist_name._pkg_version",
            side_effect=_fake_pkg_version({"claude-coordinator": "1.2.3"}),
        ):
            assert resolve_installed_name() == "claude-coordinator"

    def test_returns_none_rather_than_raising_when_neither_installed(self) -> None:
        with patch("coord.dist_name._pkg_version", side_effect=_fake_pkg_version({})):
            assert resolve_installed_name() is None


class TestPkgSpec:
    """`/update`'s pip install target (`coord.agent_app._agent_pkg_spec`,
    née the hardcoded `AGENT_PKG_NAME`)."""

    def test_appends_extra_to_whichever_name_resolved(self) -> None:
        with patch(
            "coord.dist_name._pkg_version",
            side_effect=_fake_pkg_version({"code-coordinator": "4.5.6"}),
        ):
            assert pkg_spec(extra="server") == "code-coordinator[server]"

    def test_no_extra_returns_bare_name(self) -> None:
        with patch(
            "coord.dist_name._pkg_version",
            side_effect=_fake_pkg_version({"claude-coordinator": "1.2.3"}),
        ):
            assert pkg_spec() == "claude-coordinator"

    def test_raises_rather_than_guessing_when_neither_installed(self) -> None:
        """#2103: this is the one call site that must NOT silently default
        to a literal — installing the wrong name mid-rename either 404s
        against PyPI or resurrects a stale package. The caller
        (`coord.agent_app`'s `/update` handler) already has an explicit
        failure-reporting lane for exactly this exception."""
        with patch("coord.dist_name._pkg_version", side_effect=_fake_pkg_version({})):
            with pytest.raises(DistributionNotFoundError):
                pkg_spec(extra="server")


# ── #2103 acceptance #2, verbatim: a REAL wheel built + installed under the
# new name, not a mock of importlib.metadata ────────────────────────────────


def _renamed_tagged_clone(tmp_path: Path, new_name: str, version: str) -> Path:
    """A throwaway local clone of this repo with `pyproject.toml`'s
    `[project].name` rewritten to *new_name* and tagged `v{version}` on
    HEAD — models the exact post-#2096-rename PyPI artifact, built from
    this repo's own build config rather than a synthetic stand-in."""
    clone = tmp_path / "renamed_clone"
    result = _run(
        ["git", "clone", "--quiet", "--local", "--no-tags", str(REPO_ROOT), str(clone)],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr

    pyproject = clone / "pyproject.toml"
    text = pyproject.read_text()
    old = 'name = "claude-coordinator"'
    new_text = text.replace(old, f'name = "{new_name}"', 1)
    assert new_text != text, f"{old!r} not found in pyproject.toml — update this test"
    pyproject.write_text(new_text)

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    result = _run(["git", "commit", "-aqm", f"rename to {new_name}"], cwd=clone, env=env)
    assert result.returncode == 0, result.stderr
    result = _run(["git", "tag", f"v{version}"], cwd=clone)
    assert result.returncode == 0, result.stderr
    return clone


def _install_wheel_to_target(wheel: Path, target_dir: Path) -> None:
    result = _run(
        [
            sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
            "--target", str(target_dir), str(wheel),
        ],
        cwd=target_dir.parent,
    )
    assert result.returncode == 0, f"wheel install failed:\n{result.stdout}\n{result.stderr}"


class TestBuildUnderNewName:
    """#2103 acceptance #2, verbatim: build a wheel named `code-coordinator`
    (standing in for #2096's eventual PyPI artifact), install it, and prove
    `coord.dist_name.resolve_installed()` finds it for real."""

    def test_resolves_a_real_wheel_installed_under_the_new_name(self, tmp_path: Path) -> None:
        clone = _renamed_tagged_clone(tmp_path, "code-coordinator", "8.8.8")
        wheel = _build_wheel(clone, tmp_path / "dist")
        assert "code_coordinator" in wheel.name

        install_dir = tmp_path / "install"
        install_dir.mkdir()
        _install_wheel_to_target(wheel, install_dir)

        # Run from a directory with no `coord/` of its own, same reasoning
        # as test_version_single_source.py's `_cli_version_from_wheel`:
        # sys.path[0] (the cwd `-c` adds) must not shadow the installed copy
        # with this checkout's own source tree.
        neutral_cwd = tmp_path / "cwd"
        neutral_cwd.mkdir()
        env = dict(os.environ)
        env["PYTHONPATH"] = str(install_dir)
        result = _run(
            [
                sys.executable, "-c",
                "from coord.dist_name import resolve_installed\n"
                "r = resolve_installed()\n"
                "print(f'{r.name} {r.version}')\n",
            ],
            cwd=neutral_cwd,
            env=env,
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert result.stdout.strip() == "code-coordinator 8.8.8"


class TestAgentUpdateSmokeCheckUsesThisModule:
    """#2103 site 4 (`coord/agent_update.py`'s smoke check): the embedded
    `python -c` script now imports `coord.dist_name` instead of hardcoding
    `m.version('claude-coordinator')`. Run the *real* script against a real
    interpreter (no `subprocess.run` stub, unlike
    `tests/test_agent_update_bluegreen.py`) so a typo/syntax error in that
    embedded string is caught here rather than only by a stub that never
    actually executes it."""

    def test_real_subprocess_reports_the_installed_version(self, tmp_path: Path) -> None:
        from coord.agent_update import _smoke_check

        coord_console_script = Path(sys.executable).parent / "coord"
        if not coord_console_script.exists():
            pytest.skip("no `coord` console script next to sys.executable in this env")

        slot = tmp_path / "slot"
        (slot / "bin").mkdir(parents=True)
        (slot / "bin" / "python").symlink_to(sys.executable)
        (slot / "bin" / "coord").symlink_to(coord_console_script)

        ok, detected_version, log = _smoke_check(slot, target_version=None)

        assert ok is True, log
        assert detected_version == resolve_installed().version
