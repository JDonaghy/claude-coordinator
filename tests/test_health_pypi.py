"""PEP 503 simple-index parsing and version ordering (#1628).

The issue calls out one gotcha specifically: **poll the simple index, not the
JSON API.**  They flip independently in both directions and only the simple
index is what ``pip`` resolves against, so an answer derived from the JSON
API can say "you're current" while ``pip install -U`` disagrees — which
turns the version check into a source of confusion rather than a signal.
These tests pin the parsing of that index format.
"""

from __future__ import annotations

import pytest

from coord.health.pypi import (
    latest_release,
    normalize_name,
    parse_simple_index,
    parse_version,
    split_distribution_filename,
)

PROJECT = "code-coordinator"


def _page(*anchors: str) -> str:
    body = "".join(anchors)
    return f'<!DOCTYPE html><html><head><title>Links</title></head><body>{body}</body></html>'


def _wheel(version: str, *, yanked: bool = False, name: str = "code_coordinator") -> str:
    filename = f"{name}-{version}-py3-none-any.whl"
    attrs = ' data-yanked=""' if yanked else ""
    return f'<a href="https://files.pythonhosted.org/x/{filename}#sha256=ab"{attrs}>{filename}</a><br/>'


def _sdist(version: str, name: str = "code_coordinator") -> str:
    filename = f"{name}-{version}.tar.gz"
    return f'<a href="https://files.pythonhosted.org/x/{filename}#sha256=cd">{filename}</a><br/>'


# ── name normalisation ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("code_coordinator", "code-coordinator"),
        ("Code.Coordinator", "code-coordinator"),
        ("code--coordinator", "code-coordinator"),
        ("code-coordinator", "code-coordinator"),
        # The pre-#2104 name still normalises — a mid-rename fleet has
        # agents reporting it, and #2103's fallback resolves against it.
        ("claude_coordinator", "claude-coordinator"),
    ],
)
def test_normalize_name(raw, expected) -> None:
    assert normalize_name(raw) == expected


# ── filename splitting ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("code_coordinator-0.4.91-py3-none-any.whl", ("code_coordinator", "0.4.91")),
        # With a build tag (6 components).
        ("foo-1.2.3-1-py3-none-any.whl", ("foo", "1.2.3")),
        # A non-pure wheel — the tag isn't "py*", which a naive regex misses.
        ("foo-1.2.3-cp312-cp312-linux_x86_64.whl", ("foo", "1.2.3")),
        ("code_coordinator-0.4.91.tar.gz", ("code_coordinator", "0.4.91")),
        # sdists may keep hyphens in the name, so they split from the right.
        ("code-coordinator-0.4.91.tar.gz", ("code-coordinator", "0.4.91")),
        ("foo-1.0.zip", ("foo", "1.0")),
    ],
)
def test_split_distribution_filename(filename, expected) -> None:
    assert split_distribution_filename(filename) == expected


@pytest.mark.parametrize(
    "filename",
    ["not-a-distribution.txt", "toofew-1.0-py3.whl", "README.md", ""],
)
def test_split_distribution_filename_rejects_junk(filename) -> None:
    assert split_distribution_filename(filename) is None


# ── version ordering ─────────────────────────────────────────────────────────


def test_version_ordering_is_numeric_not_lexicographic() -> None:
    """0.4.9 < 0.4.10 — a string compare gets this backwards, and a version
    check that reports "current" when you're behind is worse than none."""
    assert parse_version("0.4.9") < parse_version("0.4.10")
    assert parse_version("0.4.91") < parse_version("0.5.0")
    assert parse_version("1.0") == parse_version("1.0.0")


def test_prerelease_ordering() -> None:
    assert parse_version("1.0.0rc1") < parse_version("1.0.0")
    assert parse_version("1.0.0.dev3") < parse_version("1.0.0rc1")
    assert parse_version("1.0.0") < parse_version("1.0.0.post1")


def test_prerelease_flag() -> None:
    assert parse_version("1.0.0rc1").is_prerelease
    assert parse_version("1.0.0.dev1").is_prerelease
    assert not parse_version("1.0.0").is_prerelease
    assert not parse_version("1.0.0.post1").is_prerelease


def test_unparseable_version_returns_none_rather_than_guessing() -> None:
    """Refusing to guess beats mis-ordering: an alert that cries wolf gets muted."""
    assert parse_version("not-a-version") is None
    assert parse_version("") is None
    assert parse_version("1.0.0+local.build") is None


# ── index parsing ────────────────────────────────────────────────────────────


def test_parse_simple_index_collects_versions_ascending() -> None:
    html = _page(_wheel("0.4.90"), _sdist("0.4.91"), _wheel("0.4.91"), _wheel("0.4.89"))
    versions = [v.raw for v in parse_simple_index(html, PROJECT)]
    assert versions == ["0.4.89", "0.4.90", "0.4.91"]


def test_parse_simple_index_excludes_yanked_releases() -> None:
    """pip won't resolve to a yanked release, so counting one as "you're
    behind" would be a lie the operator can't act on."""
    html = _page(_wheel("0.4.90"), _wheel("0.4.91", yanked=True))
    assert [v.raw for v in parse_simple_index(html, PROJECT)] == ["0.4.90"]


def test_parse_simple_index_ignores_other_projects() -> None:
    html = _page(_wheel("0.4.91"), _wheel("9.9.9", name="something_else"))
    assert [v.raw for v in parse_simple_index(html, PROJECT)] == ["0.4.91"]


def test_parse_simple_index_matches_normalized_names() -> None:
    """The index serves ``code_coordinator-*`` files for ``code-coordinator``."""
    html = _page(_wheel("0.4.91", name="Code.Coordinator"))
    assert [v.raw for v in parse_simple_index(html, PROJECT)] == ["0.4.91"]


def test_parse_simple_index_tolerates_garbage() -> None:
    assert parse_simple_index("", PROJECT) == []
    assert parse_simple_index("<html><body>nothing here</body></html>", PROJECT) == []
    assert parse_simple_index("not html at all", PROJECT) == []


def test_parse_simple_index_dedupes_wheel_and_sdist_of_one_release() -> None:
    html = _page(_wheel("0.4.91"), _sdist("0.4.91"))
    assert [v.raw for v in parse_simple_index(html, PROJECT)] == ["0.4.91"]


def test_latest_release_excludes_prereleases(monkeypatch) -> None:
    """`pip install -U` won't pick a pre-release, so "behind" must not count one."""
    html = _page(_wheel("0.4.91"), _wheel("0.5.0rc1"))
    monkeypatch.setattr(
        "coord.health.pypi.fetch_simple_index", lambda p, **k: html
    )
    latest, finals = latest_release(PROJECT)
    assert latest.raw == "0.4.91"
    assert [v.raw for v in finals] == ["0.4.91"]


def test_latest_release_on_an_empty_index(monkeypatch) -> None:
    monkeypatch.setattr("coord.health.pypi.fetch_simple_index", lambda p, **k: _page())
    latest, finals = latest_release(PROJECT)
    assert latest is None and finals == []


def test_fetch_uses_the_simple_index_url_not_the_json_api(monkeypatch) -> None:
    """The gotcha, pinned: the URL must be the PEP 503 index path."""
    seen: dict[str, object] = {}

    class _Response:
        text = "<html></html>"

        def raise_for_status(self):
            return None

    def _get(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return _Response()

    monkeypatch.setattr("httpx.get", _get)
    from coord.health.pypi import fetch_simple_index

    fetch_simple_index(PROJECT, index_url="https://pypi.org/simple", timeout=1.5)
    assert seen["url"] == "https://pypi.org/simple/code-coordinator/"
    assert "/pypi/" not in str(seen["url"]), "that would be the JSON API"
    assert "json" not in str(seen["url"])
    assert seen["timeout"] == 1.5
