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
    assert "`a` (local x123456" in body
    assert "`b` is **dirty**" in body
