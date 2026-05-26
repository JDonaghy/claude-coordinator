"""Tests for coord.freshness — repo HEAD comparison logic."""

from __future__ import annotations

import pytest

from coord.config import Config
from coord.freshness import (
    CURRENT,
    DIRTY,
    MISSING,
    STALE,
    UNKNOWN,
    compare,
    dependency_freshness,
    format_briefing_addendum,
    stale_or_dirty,
)
from coord.models import Proposal, Repo


def test_compare_current() -> None:
    local = {"sha": "aaa111", "branch": "main", "dirty": False}
    f = compare("api", local, "aaa111")
    assert f.state == CURRENT
    assert not f.dirty


def test_compare_stale() -> None:
    local = {"sha": "aaa111", "branch": "main", "dirty": False}
    f = compare("api", local, "bbb222")
    assert f.state == STALE
    assert f.local_sha == "aaa111"
    assert f.remote_sha == "bbb222"


def test_compare_dirty_wins_over_stale() -> None:
    local = {"sha": "aaa", "branch": "main", "dirty": True}
    f = compare("api", local, "bbb")
    assert f.state == DIRTY
    assert f.dirty is True


def test_compare_agent_error_marks_missing() -> None:
    local = {"error": "no repo_path configured"}
    f = compare("api", local, "abc")
    assert f.state == MISSING
    assert "no repo_path" in f.error


def test_compare_remote_unknown() -> None:
    local = {"sha": "abc", "branch": "main", "dirty": False}
    f = compare("api", local, None)
    assert f.state == UNKNOWN
    assert "remote head" in f.error


def test_compare_local_missing() -> None:
    f = compare("api", None, "abc")
    assert f.state == MISSING
    assert f.error == "agent did not report this repo"


def _proposal(repo_name: str = "vimcode") -> Proposal:
    return Proposal(
        id=1, machine_name="m", repo_name=repo_name,
        issue_number=1, issue_title="t", rationale="",
    )


def _config(repos: list[Repo]) -> Config:
    return Config(repos=repos, machines=[])


def test_dependency_freshness_walks_transitive_deps() -> None:
    cfg = _config(
        [
            Repo(name="vimcode", github="acme/vimcode", depends_on=["quadraui"]),
            Repo(name="quadraui", github="acme/quadraui", depends_on=["shared"]),
            Repo(name="shared", github="acme/shared"),
        ]
    )
    repo_heads = {
        "quadraui": {"sha": "qq", "branch": "main", "dirty": False},
        "shared": {"sha": "ss", "branch": "main", "dirty": False},
    }
    github = {"quadraui": "qq", "shared": "tt"}  # shared is stale

    fs = dependency_freshness(_proposal(), cfg, repo_heads, github)
    by_repo = {f.repo_name: f for f in fs}
    assert by_repo["quadraui"].state == CURRENT
    assert by_repo["shared"].state == STALE


def test_dependency_freshness_returns_empty_for_leaf_repo() -> None:
    cfg = _config([Repo(name="standalone", github="acme/x")])
    fs = dependency_freshness(_proposal("standalone"), cfg, {}, {})
    assert fs == []


def test_stale_or_dirty_filters() -> None:
    cfg = _config(
        [
            Repo(name="vimcode", github="acme/v", depends_on=["a", "b", "c"]),
            Repo(name="a", github="acme/a"),
            Repo(name="b", github="acme/b"),
            Repo(name="c", github="acme/c"),
        ]
    )
    repo_heads = {
        "a": {"sha": "x", "branch": "main", "dirty": False},
        "b": {"sha": "y", "branch": "main", "dirty": True},
        # c missing
    }
    github = {"a": "x", "b": "y", "c": "z"}
    fs = dependency_freshness(_proposal(), cfg, repo_heads, github)
    needs = stale_or_dirty(fs)
    states = {f.repo_name: f.state for f in needs}
    assert states == {"b": DIRTY, "c": MISSING}


def test_briefing_addendum_empty_when_all_current() -> None:
    fs = [compare("a", {"sha": "x", "branch": "main", "dirty": False}, "x")]
    assert format_briefing_addendum(fs) == ""


def test_briefing_addendum_mentions_stale_repos() -> None:
    fs = [
        compare("a", {"sha": "x1234567890", "branch": "main", "dirty": False}, "y9876543210"),
        compare("b", {"sha": "abc", "branch": "main", "dirty": True}, "def"),
    ]
    body = format_briefing_addendum(fs)
    assert "Stale dependencies" in body
    # #268: build deps carry a `*(build dep)*` tag; reference repos carry
    # a `*(reference)*` tag.
    assert "`a` *(build dep)*" in body
    assert "local x123456" in body
    assert "`b` *(build dep)*" in body
    assert "**dirty**" in body


# ── #268: reference_repos ────────────────────────────────────────────────────


def test_relevant_repos_includes_transitive_build_deps_and_direct_references() -> None:
    from coord.freshness import BUILD, REFERENCE, relevant_repos

    cfg = _config(
        [
            Repo(
                name="quadraui",
                github="acme/quadraui",
                depends_on=["shared"],
                reference_repos=["vimcode"],
            ),
            Repo(name="vimcode", github="acme/vimcode"),
            Repo(name="shared", github="acme/shared"),
        ]
    )
    pairs = relevant_repos(_proposal("quadraui"), cfg)
    assert (("shared", BUILD)) in pairs
    assert (("vimcode", REFERENCE)) in pairs


def test_relevant_repos_dedupes_when_repo_is_both_dep_and_reference() -> None:
    """If the same name appears in both `depends_on` and `reference_repos`,
    the stricter `BUILD` tag wins so the briefing doesn't double-tag it."""
    from coord.freshness import BUILD, relevant_repos

    cfg = _config(
        [
            Repo(
                name="a",
                github="acme/a",
                depends_on=["lib"],
                reference_repos=["lib"],  # duplicate
            ),
            Repo(name="lib", github="acme/lib"),
        ]
    )
    pairs = relevant_repos(_proposal("a"), cfg)
    assert pairs.count(("lib", BUILD)) == 1
    # No REFERENCE entry for `lib` — dedup'd.
    kinds_for_lib = [k for n, k in pairs if n == "lib"]
    assert kinds_for_lib == [BUILD]


def test_dependency_freshness_includes_reference_repos() -> None:
    cfg = _config(
        [
            Repo(name="quadraui", github="acme/quadraui", reference_repos=["vimcode"]),
            Repo(name="vimcode", github="acme/vimcode"),
        ]
    )
    repo_heads = {"vimcode": {"sha": "OLD", "branch": "main", "dirty": False}}
    github: dict[str, str | None] = {"vimcode": "NEW"}

    fs = dependency_freshness(_proposal("quadraui"), cfg, repo_heads, github)
    by_repo = {f.repo_name: f for f in fs}
    assert "vimcode" in by_repo
    assert by_repo["vimcode"].state == STALE
    # Tagged as a reference (not a build dep) so the briefing knows.
    from coord.freshness import REFERENCE
    assert by_repo["vimcode"].kind == REFERENCE


def test_briefing_addendum_labels_reference_repos() -> None:
    from coord.freshness import REFERENCE

    fs = [
        compare(
            "vimcode",
            {"sha": "OLD12345678", "branch": "main", "dirty": False},
            "NEW98765432",
            kind=REFERENCE,
        ),
    ]
    body = format_briefing_addendum(fs)
    assert "`vimcode` *(reference)*" in body
    # And the explanatory blurb mentions references.
    assert "siblings" in body.lower() or "context" in body.lower()


def test_reference_repos_does_not_trip_cycle_detector() -> None:
    """The motivating case: vimcode depends on quadraui (build dep), and
    quadraui lists vimcode as a reference (sibling-extracted-from).
    The cycle detector must NOT fire on this — that's precisely why
    reference_repos is a separate field from depends_on."""
    from coord.config import _validate_dependencies

    repos = [
        Repo(name="vimcode", github="acme/vimcode", depends_on=["quadraui"]),
        Repo(
            name="quadraui",
            github="acme/quadraui",
            depends_on=[],
            reference_repos=["vimcode"],
        ),
    ]
    # Should not raise.
    _validate_dependencies(repos)


def test_reference_repos_unknown_entry_raises() -> None:
    from coord.config import ConfigError, _validate_dependencies

    repos = [
        Repo(
            name="quadraui",
            github="acme/quadraui",
            reference_repos=["nonexistent"],
        ),
    ]
    import pytest
    with pytest.raises(ConfigError, match="reference_repos unknown"):
        _validate_dependencies(repos)


def test_reference_repos_self_reference_raises() -> None:
    from coord.config import ConfigError, _validate_dependencies

    repos = [
        Repo(name="a", github="acme/a", reference_repos=["a"]),
    ]
    import pytest
    with pytest.raises(ConfigError, match="cannot reference itself"):
        _validate_dependencies(repos)
