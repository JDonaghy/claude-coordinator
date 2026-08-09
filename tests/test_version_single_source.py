"""Tests for #1238: the git tag is the single version source (setuptools-scm),
and #2010: an *editable* install must not trust a `.dist-info` stamped at
`pip install -e .` time and never refreshed since.

Three layers:

* ``TestVersionMetadataFallback`` — fast unit tests of `coord/__init__.py`'s
  own fallback logic (`importlib.metadata.version(...)` -> `"0+unknown"`
  when the package isn't installed at all), via `importlib.reload` with a
  mocked `importlib.metadata.version`. No git/build involved — this is the
  regression guard for "someone reintroduces a hardcoded `__version__`
  literal" or "the `PackageNotFoundError` fallback gets deleted". These
  also stub out `Distribution.from_name` so the test is isolated from
  whether *this* interpreter happens to have `claude-coordinator` installed
  editable (the normal `pip install -e ".[dev]"` dev/CI setup) — without
  that stub, `_resolve_version`'s #2010 editable-override path would kick
  in for real and clobber the mocked metadata version being tested here.

* ``TestEditableSourceRoot`` / ``TestLiveScmVersion`` / ``TestResolveVersion``
  — unit tests for the #2010 editable-install override: detecting an
  editable install via `direct_url.json`, preferring a live git-derived
  version over the frozen `.dist-info` metadata, and the two-tier fallback
  (`setuptools_scm.get_version()` when importable, else `git describe`)
  degrading to the stale metadata only when both fail.

* ``TestBuildFromGitTag`` — the black-box acceptance criteria from #1238
  itself, verbatim: build a real wheel from a tagged tree and assert
  `coord --version` == the tag (minus the `v`); build from a tagless tree
  and assert a `.devN+g<sha>` string comes out instead of a crash. This is
  the regression guard for `[tool.setuptools_scm]`/`fallback_version` being
  broken in `pyproject.toml`.

The build tests reuse the same local-clone git fixture style as
tests/test_cli_release_preflight.py (a throwaway repo built from real `git`
commands, no network), but tag/untag a clone of *this* repo rather than
building a synthetic one from scratch, since the thing under test is
`pyproject.toml`'s actual `[tool.setuptools_scm]` config, not a stand-in.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import coord
from coord import _editable_source_root, _live_scm_version, _resolve_version

REPO_ROOT = Path(__file__).resolve().parents[1]


def _not_editable():
    """Patch `Distribution.from_name` so `_editable_source_root` sees "no
    metadata for this package" — isolates a test from whatever install
    mode *this* interpreter's `claude-coordinator` actually happens to be
    in (editable dev/CI setups are common and would otherwise make the
    #2010 override kick in for real during these metadata-only tests)."""
    from importlib.metadata import PackageNotFoundError

    return patch(
        "importlib.metadata.Distribution.from_name",
        side_effect=PackageNotFoundError("claude-coordinator"),
    )


class TestVersionMetadataFallback:
    """`coord.__version__` must be read from installed package metadata —
    never a hardcoded literal — and degrade to `"0+unknown"` rather than
    raising when the package isn't installed at all."""

    def teardown_method(self) -> None:
        # Every test here reloads `coord` with a patched
        # `importlib.metadata.version`; put the real module state back so
        # nothing later in the suite observes a mocked __version__.
        importlib.reload(coord)

    def test_version_comes_from_installed_metadata(self) -> None:
        with patch("importlib.metadata.version", return_value="7.8.9"), _not_editable():
            importlib.reload(coord)
            assert coord.__version__ == "7.8.9"

    def test_falls_back_when_package_not_installed(self) -> None:
        from importlib.metadata import PackageNotFoundError

        def _raise(name: str) -> str:
            raise PackageNotFoundError(name)

        with patch("importlib.metadata.version", side_effect=_raise), _not_editable():
            importlib.reload(coord)
            assert coord.__version__ == "0+unknown"

    def test_queries_metadata_for_this_exact_package_name(self) -> None:
        """A typo'd or renamed lookup key would silently always hit the
        PackageNotFoundError fallback and no test would notice — pin the
        argument, not just the outcome."""
        with patch("importlib.metadata.version", return_value="1.2.3") as mock_version, \
                _not_editable():
            importlib.reload(coord)
            mock_version.assert_called_once_with("claude-coordinator")


class TestEditableSourceRoot:
    """#2010: `_editable_source_root` must recognize an editable install
    only from pip's own `direct_url.json` signal (`dir_info.editable:
    true` + a `file://` URL) — never from a looser heuristic like
    `__file__` containing "site-packages", which a non-editable venv
    install also satisfies and whose metadata IS trustworthy."""

    def test_returns_root_for_editable_install(self, tmp_path: Path) -> None:
        root = tmp_path / "checkout"
        root.mkdir()
        direct_url = json.dumps({"url": f"file://{root}", "dir_info": {"editable": True}})
        fake_dist = SimpleNamespace(read_text=lambda name: direct_url)
        with patch("importlib.metadata.Distribution.from_name", return_value=fake_dist):
            assert _editable_source_root("claude-coordinator") == root

    def test_none_for_non_editable_install(self, tmp_path: Path) -> None:
        root = tmp_path / "checkout"
        root.mkdir()
        # pip writes direct_url.json for non-editable source/wheel installs
        # too, just without `dir_info.editable` — that must NOT trigger
        # the live-version override, or a frozen non-editable snapshot
        # would start reading live git state it has nothing to do with.
        direct_url = json.dumps({"url": f"file://{root}"})
        fake_dist = SimpleNamespace(read_text=lambda name: direct_url)
        with patch("importlib.metadata.Distribution.from_name", return_value=fake_dist):
            assert _editable_source_root("claude-coordinator") is None

    def test_none_when_no_direct_url_json(self) -> None:
        # A normal PyPI wheel install has no direct_url.json at all.
        fake_dist = SimpleNamespace(read_text=lambda name: None)
        with patch("importlib.metadata.Distribution.from_name", return_value=fake_dist):
            assert _editable_source_root("claude-coordinator") is None

    def test_none_when_distribution_not_found(self) -> None:
        with _not_editable():
            assert _editable_source_root("claude-coordinator") is None

    def test_none_when_editable_root_missing_from_disk(self, tmp_path: Path) -> None:
        missing = tmp_path / "gone"
        direct_url = json.dumps({"url": f"file://{missing}", "dir_info": {"editable": True}})
        fake_dist = SimpleNamespace(read_text=lambda name: direct_url)
        with patch("importlib.metadata.Distribution.from_name", return_value=fake_dist):
            assert _editable_source_root("claude-coordinator") is None


class TestLiveScmVersion:
    """#2010: `_live_scm_version` prefers `setuptools_scm.get_version()`
    when importable, falls back to `git describe`, and returns `None`
    (never raises) when neither works."""

    def test_uses_setuptools_scm_when_importable(self, tmp_path: Path) -> None:
        fake_module = SimpleNamespace(get_version=lambda **kwargs: "1.2.3.dev4+gabcdef0")
        with patch.dict(sys.modules, {"setuptools_scm": fake_module}):
            assert _live_scm_version(tmp_path) == "1.2.3.dev4+gabcdef0"

    def test_falls_back_to_git_describe_when_setuptools_scm_unimportable(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _run_git(["init", "-q"], repo)
        _run_git(["config", "user.email", "a@b.c"], repo)
        _run_git(["config", "user.name", "Test"], repo)
        (repo / "f.txt").write_text("1")
        _run_git(["add", "f.txt"], repo)
        _run_git(["commit", "-q", "-m", "init"], repo)
        _run_git(["tag", "v3.4.5"], repo)

        with patch.dict(sys.modules, {"setuptools_scm": None}):
            version = _live_scm_version(repo)

        assert version == "3.4.5"

    def test_git_describe_reports_dirty_when_checkout_has_local_edits(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _run_git(["init", "-q"], repo)
        _run_git(["config", "user.email", "a@b.c"], repo)
        _run_git(["config", "user.name", "Test"], repo)
        (repo / "f.txt").write_text("1")
        _run_git(["add", "f.txt"], repo)
        _run_git(["commit", "-q", "-m", "init"], repo)
        _run_git(["tag", "v3.4.5"], repo)
        (repo / "f.txt").write_text("2")  # uncommitted local edit

        with patch.dict(sys.modules, {"setuptools_scm": None}):
            version = _live_scm_version(repo)

        assert version == "3.4.5-dirty"

    def test_none_when_not_a_git_checkout(self, tmp_path: Path) -> None:
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        with patch.dict(sys.modules, {"setuptools_scm": None}):
            assert _live_scm_version(not_a_repo) is None


def _run_git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"


class TestResolveVersion:
    """#2010: `_resolve_version` end to end — editable installs prefer a
    live version over stale metadata; non-editable installs never
    consult git at all."""

    def test_non_editable_install_ignores_live_version_entirely(self, tmp_path: Path) -> None:
        """A non-editable install must not even attempt a live lookup — if
        it did, this test's `_live_scm_version` stub returning a live
        value instead of the metadata would go unnoticed."""
        with patch("coord._pkg_version", return_value="0.5.1"), _not_editable(), \
                patch("coord._live_scm_version", return_value="9.9.9-should-not-be-used"):
            assert _resolve_version("claude-coordinator") == "0.5.1"

    def test_editable_install_prefers_live_version_over_stale_metadata(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "checkout"
        root.mkdir()
        with patch("coord._pkg_version", return_value="0.1.0"), \
                patch("coord._editable_source_root", return_value=root), \
                patch("coord._live_scm_version", return_value="0.5.1"):
            assert _resolve_version("claude-coordinator") == "0.5.1"

    def test_editable_install_falls_back_to_metadata_when_live_lookup_fails(
        self, tmp_path: Path
    ) -> None:
        """git isn't installed, or the checkout has no reachable tags at
        all — `_live_scm_version` returns `None` and we still surface
        *something* rather than raising, even though it may be stale."""
        root = tmp_path / "checkout"
        root.mkdir()
        with patch("coord._pkg_version", return_value="0.1.0"), \
                patch("coord._editable_source_root", return_value=root), \
                patch("coord._live_scm_version", return_value=None):
            assert _resolve_version("claude-coordinator") == "0.1.0"


def _require_build_backend() -> None:
    """Skip (don't fail) when the build backend isn't importable here.

    The `dev` extra installs setuptools/setuptools-scm/wheel precisely so
    these tests run in the documented `pip install -e ".[dev]"` venv and in
    CI, which is where the regression guard has to bite. This guard only
    covers a hand-rolled environment that installed pytest without the
    extra: skipping there beats a `--no-build-isolation` failure that looks
    like a version-derivation bug but isn't.
    """
    for mod in ("setuptools", "setuptools_scm", "wheel"):
        pytest.importorskip(
            mod,
            reason=(
                f"{mod} is not importable in this interpreter; install the dev "
                'extra (`pip install -e ".[dev]"`) to run the wheel-build tests'
            ),
        )


def _run(cmd: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=120, env=env
    )


@pytest.fixture
def tagged_clone(tmp_path: Path) -> Path:
    """A throwaway local clone of this repo, cloned with no tags and then
    given exactly one synthetic `v9.9.9` tag on HEAD — models a tagged
    release checkout without depending on this repo's real (and
    ever-changing) tag history."""
    clone = tmp_path / "tagged_clone"
    result = _run(
        ["git", "clone", "--quiet", "--local", "--no-tags", str(REPO_ROOT), str(clone)],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    result = _run(["git", "tag", "v9.9.9"], cwd=clone)
    assert result.returncode == 0, result.stderr
    return clone


@pytest.fixture
def tagless_clone(tmp_path: Path) -> Path:
    """A throwaway local clone of this repo with no tags reachable at
    all — models a shallow/tagless CI checkout."""
    clone = tmp_path / "tagless_clone"
    result = _run(
        ["git", "clone", "--quiet", "--local", "--no-tags", str(REPO_ROOT), str(clone)],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    return clone


def _build_wheel(repo_dir: Path, out_dir: Path) -> Path:
    """Build a wheel from *repo_dir*, the same build backend the publish
    workflow's `python -m build` invokes.

    `--no-build-isolation` builds against *this* interpreter's installed
    setuptools/wheel/setuptools-scm rather than letting pip create a
    throwaway build env, so the build needs no network access and no
    per-test venv rebuild. That only works because the `dev` extra in
    pyproject.toml installs those three packages alongside pytest —
    `[build-system].requires` alone would not, since PEP 517 discards the
    isolated env it installs them into. `_require_build_backend()` turns a
    non-standard env that lacks them into an explicit skip rather than a
    confusing "build backend unavailable" failure.
    """
    _require_build_backend()
    out_dir.mkdir(parents=True, exist_ok=True)
    result = _run(
        [
            sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
            "-w", str(out_dir), str(repo_dir),
        ],
        cwd=repo_dir,
    )
    assert result.returncode == 0, f"wheel build failed:\n{result.stdout}\n{result.stderr}"
    wheels = sorted(out_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel in {out_dir}, got {wheels}"
    return wheels[0]


def _cli_version_from_wheel(wheel: Path, work_dir: Path) -> str:
    """Install *wheel* (package files only, no deps, no network) into an
    isolated directory and invoke the real `coord.cli` module — the same
    code (`@click.version_option(__version__, ...)` reading
    `coord.__version__`) that the installed `coord` console-script runs for
    `coord --version` — addressed via `python -m` so this needs neither a
    fresh venv nor reinstalling coord's runtime dependencies."""
    install_dir = work_dir / "install"
    install_dir.mkdir(parents=True)
    result = _run(
        [
            sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
            "--target", str(install_dir), str(wheel),
        ],
        cwd=work_dir,
    )
    assert result.returncode == 0, f"wheel install failed:\n{result.stdout}\n{result.stderr}"

    # Run from a directory with no `coord/` of its own so `sys.path[0]`
    # (the empty-string cwd entry `-m` adds) can't shadow the installed one
    # with this checkout's source tree.
    neutral_cwd = work_dir / "cwd"
    neutral_cwd.mkdir()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(install_dir)
    result = _run([sys.executable, "-m", "coord.cli", "--version"], cwd=neutral_cwd, env=env)
    assert result.returncode == 0, f"coord --version failed:\n{result.stdout}\n{result.stderr}"

    match = re.search(r"coord, version (\S+)", result.stdout)
    assert match, f"unexpected --version output: {result.stdout!r}"
    return match.group(1)


class TestBuildFromGitTag:
    """The #1238 acceptance criteria, verbatim: build the wheel from a
    tagged tree and assert `coord --version` == the tag (minus the `v`);
    build from an untagged/dirty tree and assert it produces a
    `.devN+g<sha>` string, not a crash."""

    def test_tagged_tree_version_matches_tag_exactly(
        self, tagged_clone: Path, tmp_path: Path
    ) -> None:
        wheel = _build_wheel(tagged_clone, tmp_path / "dist")
        # setuptools_scm stamps the wheel filename from the resolved
        # version too — catch a regression there even before installing it.
        assert "9.9.9" in wheel.name

        version = _cli_version_from_wheel(wheel, tmp_path / "run")

        assert version == "9.9.9"

    def test_tagless_tree_produces_dev_version_not_a_crash(
        self, tagless_clone: Path, tmp_path: Path
    ) -> None:
        wheel = _build_wheel(tagless_clone, tmp_path / "dist")

        version = _cli_version_from_wheel(wheel, tmp_path / "run")

        assert re.match(r"^\d+(\.\d+)*\.dev\d+\+g[0-9a-f]+$", version), version
