"""Tests for `scripts/gen_mock_index.py` (#2512): the deterministic
post-render step that turns a flat `mocks/*.html` directory into a
navigable set by writing `mocks/index.html`.

See `coord/mock_author.py`'s `_wants_mock_index` / `_mock_index_instruction`
for where the mock-author worker is told to run this script — these tests
cover the script's own generation logic in isolation, without a worker or
a live milestone dir.
"""
from __future__ import annotations

from pathlib import Path

from scripts.gen_mock_index import generate_index, index_mock_files, main


def _write(path: Path, title: str | None, body: str = "<p>hi</p>") -> None:
    head = f"<head><title>{title}</title></head>" if title is not None else "<head></head>"
    path.write_text(f"<html>{head}<body>{body}</body></html>", encoding="utf-8")


def test_index_mock_files_excludes_index_itself_and_sorts(tmp_path: Path):
    mocks = tmp_path / "mocks"
    mocks.mkdir()
    _write(mocks / "b.html", "B")
    _write(mocks / "a.html", "A")
    _write(mocks / "index.html", "should be excluded")

    files = index_mock_files(mocks)

    assert [p.name for p in files] == ["a.html", "b.html"]


def test_generate_index_links_each_mock_by_its_title(tmp_path: Path):
    mocks = tmp_path / "mocks"
    mocks.mkdir()
    _write(mocks / "reports-grid.html", "coord web — Reports panel (ms-2 Gate-A mock)")
    _write(mocks / "reports-chart.html", "coord web — Reports chart (ms-2 Gate-A mock)")

    out = generate_index(mocks)

    assert '<a href="reports-grid.html">coord web — Reports panel (ms-2 Gate-A mock)</a>' in out
    assert '<a href="reports-chart.html">coord web — Reports chart (ms-2 Gate-A mock)</a>' in out


def test_generate_index_falls_back_to_filename_when_title_missing(tmp_path: Path):
    mocks = tmp_path / "mocks"
    mocks.mkdir()
    _write(mocks / "untitled.html", None)

    out = generate_index(mocks)

    assert '<a href="untitled.html">untitled.html</a>' in out


def test_generate_index_escapes_title_html(tmp_path: Path):
    mocks = tmp_path / "mocks"
    mocks.mkdir()
    _write(mocks / "weird.html", "Reports <beta> & friends")

    out = generate_index(mocks)

    assert "Reports &lt;beta&gt; &amp; friends" in out
    assert "<beta>" not in out


def test_generate_index_reports_no_mocks_found_when_dir_empty(tmp_path: Path):
    mocks = tmp_path / "mocks"
    mocks.mkdir()

    out = generate_index(mocks)

    assert "(no mocks found)" in out


def test_generate_index_is_deterministic_across_runs(tmp_path: Path):
    mocks = tmp_path / "mocks"
    mocks.mkdir()
    _write(mocks / "b.html", "B")
    _write(mocks / "a.html", "A")

    assert generate_index(mocks) == generate_index(mocks)


def test_main_writes_index_html_file(tmp_path: Path):
    mocks = tmp_path / "mocks"
    mocks.mkdir()
    _write(mocks / "only.html", "Only mock")

    rc = main(["gen_mock_index.py", str(mocks)])

    assert rc == 0
    index_path = mocks / "index.html"
    assert index_path.exists()
    assert "Only mock" in index_path.read_text(encoding="utf-8")


def test_main_reruns_are_idempotent(tmp_path: Path):
    """Re-rendering an unchanged mock set must produce a byte-identical
    index — no drift between successive `--amend` passes."""
    mocks = tmp_path / "mocks"
    mocks.mkdir()
    _write(mocks / "only.html", "Only mock")

    main(["gen_mock_index.py", str(mocks)])
    first = (mocks / "index.html").read_text(encoding="utf-8")
    main(["gen_mock_index.py", str(mocks)])
    second = (mocks / "index.html").read_text(encoding="utf-8")

    assert first == second


def test_main_rejects_missing_directory(tmp_path: Path, capsys):
    rc = main(["gen_mock_index.py", str(tmp_path / "nope")])

    assert rc == 1
    assert "not a directory" in capsys.readouterr().err


def test_main_requires_exactly_one_argument(capsys):
    rc = main(["gen_mock_index.py"])

    assert rc == 2
    assert "usage:" in capsys.readouterr().err
