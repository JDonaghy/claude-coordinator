"""Tests for the source-vs-install drift warning added in #222.

The CLI warns the user when it's running from a non-editable install
(site-packages snapshot) but a source checkout exists at cwd — exactly the
state that made `coord notify` and `python -c "from coord.notify import run"`
diverge in #222.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from coord.cli import main


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
    assert "non-editable install" in result.stderr
    assert "pip install -e ." in result.stderr


def test_no_warning_for_editable_install(tmp_path: Path) -> None:
    """Editable install (source IS the import path) → silent."""
    result = _run_with_fake_install(
        coord_file=str(tmp_path / "workdir" / "coord" / "__init__.py"),
        cwd_has_source=True,
        tmp_path=tmp_path,
    )
    assert result.exit_code == 0
    assert "non-editable" not in result.stderr


def test_no_warning_when_not_in_source_checkout(tmp_path: Path) -> None:
    """Non-editable install but cwd has no coord/ dir → silent (legitimate
    user-mode install, not a dev workflow)."""
    result = _run_with_fake_install(
        coord_file="/some/venv/lib/python3.12/site-packages/coord/__init__.py",
        cwd_has_source=False,
        tmp_path=tmp_path,
    )
    assert result.exit_code == 0
    assert "non-editable" not in result.stderr
