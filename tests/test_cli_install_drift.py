"""Tests for the source-vs-install drift warning added in #222, plus the
staleness-quantification escalation added in #1182.

The CLI warns the user when it's running from a non-editable install
(site-packages snapshot) but a source checkout exists at cwd — exactly the
state that made `coord notify` and `python -c "from coord.notify import run"`
diverge in #222. #1182 found that plain warning reads as boilerplate noise
regardless of how stale the install actually is — a real incident on
elitebook had the installed version many commits/releases behind `main`,
silently evaluating retired logic and causing a false merge-gate block. When
the checkout has a tag matching the installed version, the CLI now escalates
to a much louder "STALE INSTALL" banner once the checkout's HEAD has moved
`_STALE_COMMITS_THRESHOLD` commits or `_STALE_DAYS_THRESHOLD` days past it.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from coord.cli import main

from .conftest import output_and_stderr


def _run_with_fake_install(coord_file: str, cwd_has_source: bool, tmp_path: Path):
    """Invoke `coord version` with a patched coord module attribute and cwd."""
    fake_coord = SimpleNamespace(__file__=coord_file, __version__="0.4.4")
    runner = CliRunner()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    if cwd_has_source:
        (workdir / "coord").mkdir()
        (workdir / "coord" / "__init__.py").write_text('__version__ = "0.4.4"\n')

    prev_cwd = os.getcwd()
    os.chdir(workdir)
    try:
        with patch.dict("sys.modules", {"coord": fake_coord}):
            return runner.invoke(main, ["version"])
    finally:
        os.chdir(prev_cwd)


def test_warning_fires_when_non_editable_inside_source_checkout(tmp_path: Path) -> None:
    """Site-packages install + cwd has a checked-out coord/ → warn (#222)."""
    result = _run_with_fake_install(
        coord_file="/some/venv/lib/python3.12/site-packages/coord/__init__.py",
        cwd_has_source=True,
        tmp_path=tmp_path,
    )
    assert result.exit_code == 0
    text = output_and_stderr(result)
    assert "non-editable install" in text
    assert "pip install -e ." in text


def test_no_warning_for_editable_install(tmp_path: Path) -> None:
    """Editable install (source IS the import path) → silent."""
    result = _run_with_fake_install(
        coord_file=str(tmp_path / "workdir" / "coord" / "__init__.py"),
        cwd_has_source=True,
        tmp_path=tmp_path,
    )
    assert result.exit_code == 0
    assert "non-editable" not in output_and_stderr(result)


def test_no_warning_when_not_in_source_checkout(tmp_path: Path) -> None:
    """Non-editable install but cwd has no coord/ dir → silent (legitimate
    user-mode install, not a dev workflow)."""
    result = _run_with_fake_install(
        coord_file="/some/venv/lib/python3.12/site-packages/coord/__init__.py",
        cwd_has_source=False,
        tmp_path=tmp_path,
    )
    assert result.exit_code == 0
    assert "non-editable" not in output_and_stderr(result)


# --- #1182: staleness quantification / escalation -------------------------


def _make_git_checkout(
    workdir: Path,
    installed_version: str,
    commits_ahead: int = 0,
    tag_age_days: float = 0.0,
) -> None:
    """Build a tiny git checkout at *workdir* with a ``v{installed_version}``
    tag, HEAD *commits_ahead* commits past that tag, and the tag's commit
    dated *tag_age_days* days in the past."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
    }

    def run(*args: str, extra_env: dict | None = None) -> None:
        subprocess.run(
            ["git", *args], cwd=str(workdir), env={**env, **(extra_env or {})},
            check=True, capture_output=True, text=True,
        )

    run("init", "-q")
    (workdir / "coord").mkdir()
    (workdir / "coord" / "__init__.py").write_text(f'__version__ = "{installed_version}"\n')
    run("add", "-A")
    tagged_ts = int(time.time() - tag_age_days * 86400)
    run(
        "commit", "-q", "-m", "initial",
        extra_env={"GIT_AUTHOR_DATE": str(tagged_ts), "GIT_COMMITTER_DATE": str(tagged_ts)},
    )
    run("tag", f"v{installed_version}")

    for i in range(commits_ahead):
        (workdir / f"file{i}.txt").write_text("x")
        run("add", "-A")
        run("commit", "-q", "-m", f"commit {i}")


def _run_with_git_checkout(
    tmp_path: Path,
    installed_version: str = "0.4.4",
    fake_version: str | None = None,
    commits_ahead: int = 0,
    tag_age_days: float = 0.0,
):
    """Build a git checkout tagged at *installed_version* and invoke `coord
    version` with the CLI reporting *fake_version* (defaults to the tag) as
    the running install — letting tests exercise a version with no matching
    tag at all."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    _make_git_checkout(workdir, installed_version, commits_ahead, tag_age_days)

    fake_coord = SimpleNamespace(
        __file__="/some/venv/lib/python3.12/site-packages/coord/__init__.py",
        __version__=fake_version if fake_version is not None else installed_version,
    )
    runner = CliRunner()
    prev_cwd = os.getcwd()
    os.chdir(workdir)
    try:
        with patch.dict("sys.modules", {"coord": fake_coord}):
            return runner.invoke(main, ["version"])
    finally:
        os.chdir(prev_cwd)


def test_stale_banner_fires_past_commit_threshold(tmp_path: Path) -> None:
    """HEAD is 5 commits past the installed version's tag (>= threshold of 3)
    → the loud STALE INSTALL banner fires instead of the mild warning."""
    result = _run_with_git_checkout(tmp_path, installed_version="0.4.4", commits_ahead=5)
    assert result.exit_code == 0
    text = output_and_stderr(result)
    assert "STALE INSTALL" in text
    assert "5 commit(s)" in text
    assert "pip install --upgrade claude-coordinator" in text
    # The escalated banner replaces the mild message, it doesn't stack with it.
    assert "Edits to the source tree will NOT reach the CLI" not in text


def test_stale_banner_fires_past_days_threshold(tmp_path: Path) -> None:
    """Only 1 commit ahead (below the commit threshold) but the tag is 5 days
    old (>= threshold of 2 days) → still escalates."""
    result = _run_with_git_checkout(
        tmp_path, installed_version="0.4.4", commits_ahead=1, tag_age_days=5,
    )
    assert result.exit_code == 0
    text = output_and_stderr(result)
    assert "STALE INSTALL" in text
    assert "1 commit(s)" in text
    assert "day(s)" in text


def test_mild_warning_when_below_both_thresholds(tmp_path: Path) -> None:
    """Only 1 commit / 0 days behind → stays on the original mild warning,
    no banner (small drift is the common, harmless case)."""
    result = _run_with_git_checkout(
        tmp_path, installed_version="0.4.4", commits_ahead=1, tag_age_days=0,
    )
    assert result.exit_code == 0
    text = output_and_stderr(result)
    assert "STALE INSTALL" not in text
    assert "non-editable install" in text


def test_mild_warning_when_no_matching_tag(tmp_path: Path) -> None:
    """Installed version doesn't correspond to any tag in the checkout (e.g.
    a dev build) → staleness can't be quantified, falls back to the mild
    warning rather than guessing or crashing."""
    result = _run_with_git_checkout(
        tmp_path, installed_version="0.4.4", fake_version="9.9.9", commits_ahead=5,
    )
    assert result.exit_code == 0
    text = output_and_stderr(result)
    assert "STALE INSTALL" not in text
    assert "non-editable install" in text
