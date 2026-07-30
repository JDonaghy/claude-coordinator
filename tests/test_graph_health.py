"""Tests for graphify graph freshness + the worktree-bootstrap hook.

Two tiers:

* **Black-box** — the acceptance bar for the user-visible behaviour here.
  ``.githooks/post-checkout`` is driven through *real git*: a real repo, a
  real ``git worktree add``, asserting on what actually lands on disk.  The
  hook is a shell script git invokes; unit-testing around it would prove
  nothing about whether git runs it or whether it does the right thing when
  it does.
* **Unit** — :mod:`coord.graph_health`'s parsing/comparison logic.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from coord.graph_health import (
    GraphStatus,
    graph_status,
    hooks_path_status,
    read_built_sha,
)

HOOK_SRC = Path(__file__).resolve().parents[1] / ".githooks" / "post-checkout"


# ── helpers ──────────────────────────────────────────────────────────────────


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30.0,
    )


def _make_repo(root: Path, *, with_hooks: bool = True) -> Path:
    """A real git repo with the versioned hook wired up via core.hooksPath.

    The hook is **committed**, not just written to the worktree.  Git resolves
    a relative ``core.hooksPath`` against the directory the git command runs
    in, so an uncommitted ``.githooks/`` still fires for a ``git worktree add``
    issued from the base — but NOT for a checkout issued from inside the
    worktree, where ``.githooks/`` wouldn't exist.  Committing it is what the
    real repo does and is what keeps the in-worktree tests below honest
    (without it they pass vacuously, asserting on a hook that never ran).
    """
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", ".", cwd=root)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=root)
    if with_hooks:
        # Install the REAL hook set, not just post-checkout: the hooks source
        # _lib.sh, and copying one file in isolation would test a shape that
        # does not exist.
        shutil.copytree(HOOK_SRC.parent, root / ".githooks")
        for p in (root / ".githooks").iterdir():
            if not p.name.endswith(".sh"):
                p.chmod(0o755)
        _git("add", "-f", ".githooks", cwd=root)
        _git("commit", "-q", "-m", "add versioned hooks", cwd=root)
        _git("config", "core.hooksPath", ".githooks", cwd=root)
    return root


def _seed_graph(repo: Path, *, built_sha: str = "deadbeef", manifest: bool = True) -> Path:
    """A minimal but realistic graphify-out/ (graph.json + report)."""
    out = repo / "graphify-out"
    out.mkdir(exist_ok=True)
    (out / "graph.json").write_text('{"nodes": []}', encoding="utf-8")
    (out / "GRAPH_REPORT.md").write_text(
        "# Graph Report\n\n"
        "## Graph Freshness\n"
        f"- Built from commit: `{built_sha}`\n",
        encoding="utf-8",
    )
    if manifest:
        (out / "manifest.json").write_text("{}", encoding="utf-8")
    return out


def _head(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)


# ── black-box: the hook, driven through real git ─────────────────────────────


def test_the_checked_in_hook_is_executable() -> None:
    """Git SILENTLY ignores a non-executable hook — it prints an advice hint at
    most, then carries on as if no hook existed.  This shipped broken once
    already (the file was created without the bit; every test still passed
    because the fixture chmod's its own copy), so assert on the real file and
    on the mode git has recorded for it."""
    assert HOOK_SRC.is_file(), f"{HOOK_SRC} is missing"
    assert os.access(HOOK_SRC, os.X_OK), (
        f"{HOOK_SRC} is not executable — git will ignore it entirely"
    )
    repo_root = HOOK_SRC.parents[1]
    tracked = subprocess.run(
        ["git", "ls-files", "-s", ".githooks/post-checkout"],
        cwd=str(repo_root), capture_output=True, text=True, timeout=10.0,
    ).stdout.strip()
    if tracked:  # skip the assertion until the file is first committed
        assert tracked.startswith("100755"), (
            f"git has the hook recorded as {tracked.split()[0]}, not mode 100755 — "
            "it will be checked out non-executable and silently ignored"
        )


def test_worktree_add_links_graphify_out_to_the_base_graph(tmp_path: Path) -> None:
    """The core behaviour: `git worktree add` leaves the worktree with a
    graphify-out symlink pointing at the base checkout's graph, so an agent
    working there can query it."""
    base = _make_repo(tmp_path / "base")
    _seed_graph(base)

    wt = tmp_path / "wt"
    res = _git("worktree", "add", "-q", "-b", "feat", str(wt), cwd=base)
    assert res.returncode == 0, res.stderr

    link = wt / "graphify-out"
    assert link.is_symlink(), "worktree graphify-out should be a symlink"
    assert link.resolve() == (base / "graphify-out").resolve()
    # And it is actually usable — the graph resolves through the link.
    assert (link / "graph.json").is_file()


def test_worktree_link_replaces_the_tracked_gitignore_stub(tmp_path: Path) -> None:
    """`graphify-out/.gitignore` is tracked, so `git worktree add` materialises
    a non-empty stub directory.  The hook must replace it, not give up."""
    base = _make_repo(tmp_path / "base")
    out = _seed_graph(base)
    (out / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")
    _git("add", "-f", "graphify-out/.gitignore", cwd=base)
    _git("commit", "-q", "-m", "track gitignore", cwd=base)

    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "feat", str(wt), cwd=base)

    link = wt / "graphify-out"
    assert link.is_symlink()
    assert (link / "graph.json").is_file()


def test_no_link_when_the_base_checkout_has_no_graph(tmp_path: Path) -> None:
    """Nothing to borrow — the hook must not create a dangling symlink."""
    base = _make_repo(tmp_path / "base")  # no _seed_graph

    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "feat", str(wt), cwd=base)

    link = wt / "graphify-out"
    assert not link.is_symlink()


def test_a_real_graph_in_the_worktree_is_never_clobbered(tmp_path: Path) -> None:
    """If a worktree somehow has its own built graph, the hook leaves it be
    rather than replacing real data with a link."""
    base = _make_repo(tmp_path / "base")
    _seed_graph(base)
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "feat", str(wt), cwd=base)
    # Replace the link with a real graph, then switch branches to re-fire.
    link = wt / "graphify-out"
    if link.is_symlink():
        link.unlink()
    own = wt / "graphify-out"
    own.mkdir(exist_ok=True)
    (own / "graph.json").write_text('{"nodes": ["mine"]}', encoding="utf-8")

    _git("checkout", "-q", "-b", "feat2", cwd=wt)

    assert not own.is_symlink()
    assert "mine" in (own / "graph.json").read_text(encoding="utf-8")


def test_main_worktree_chains_to_the_machine_local_graphify_hook(
    tmp_path: Path,
) -> None:
    """The versioned hook must not swallow graphify's own hook: in the MAIN
    worktree it execs $GIT_COMMON_DIR/hooks/post-checkout, which is where
    `graphify hook install` writes its machine-pinned block."""
    base = _make_repo(tmp_path / "base")
    _seed_graph(base)
    marker = tmp_path / "chained.txt"
    local_hook = base / ".git" / "hooks" / "post-checkout"
    local_hook.parent.mkdir(parents=True, exist_ok=True)
    local_hook.write_text(f"#!/bin/sh\necho chained >> {marker}\n", encoding="utf-8")
    local_hook.chmod(0o755)

    _git("checkout", "-q", "-b", "other", cwd=base)

    assert marker.is_file(), "main-worktree checkout should chain to the local hook"
    assert "chained" in marker.read_text(encoding="utf-8")


def test_linked_worktree_does_not_chain_to_the_rebuild_hook(tmp_path: Path) -> None:
    """A rebuild inside a worktree would overwrite the SHARED base graph from a
    feature-branch tree (and can die on a reaped worktree).  The hook must exit
    before chaining when it is in a linked worktree."""
    base = _make_repo(tmp_path / "base")
    _seed_graph(base)
    marker = tmp_path / "chained.txt"
    local_hook = base / ".git" / "hooks" / "post-checkout"
    local_hook.parent.mkdir(parents=True, exist_ok=True)
    local_hook.write_text(f"#!/bin/sh\necho chained >> {marker}\n", encoding="utf-8")
    local_hook.chmod(0o755)

    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "feat", str(wt), cwd=base)
    _git("checkout", "-q", "-b", "feat2", cwd=wt)

    assert not marker.exists(), "a linked worktree must never trigger a rebuild"


def test_the_hook_actually_runs_on_an_in_worktree_checkout(tmp_path: Path) -> None:
    """Anti-vacuity guard for the two tests above.

    Both assert that something does NOT happen after a checkout issued from
    inside a worktree — which is trivially true if the hook never ran at all.
    Git resolves a relative ``core.hooksPath`` against the invoking directory,
    so that is exactly what happens when ``.githooks/`` is not committed.
    Prove the hook is reachable there before trusting those assertions.
    """
    base = _make_repo(tmp_path / "base")
    _seed_graph(base)
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "feat", str(wt), cwd=base)
    assert (wt / ".githooks" / "post-checkout").is_file(), (
        "the worktree must carry the hook, or the in-worktree tests are vacuous"
    )

    # Remove the link, then switch branches from inside the worktree: the hook
    # must run and recreate it.
    (wt / "graphify-out").unlink()
    _git("checkout", "-q", "-b", "feat3", cwd=wt)
    assert (wt / "graphify-out").is_symlink(), "hook did not run in the worktree"


# ── unit: freshness detection ────────────────────────────────────────────────


def test_read_built_sha_parses_the_report_line(tmp_path: Path) -> None:
    report = tmp_path / "GRAPH_REPORT.md"
    report.write_text(
        "# Graph Report - x\n\n## Graph Freshness\n"
        "- Built from commit: `5be69d08`\n"
        "- Run `git rev-parse HEAD` and compare.\n",
        encoding="utf-8",
    )
    assert read_built_sha(report) == "5be69d08"


def test_read_built_sha_returns_none_when_absent(tmp_path: Path) -> None:
    report = tmp_path / "GRAPH_REPORT.md"
    report.write_text("# Graph Report\nno freshness line here\n", encoding="utf-8")
    assert read_built_sha(report) is None
    assert read_built_sha(tmp_path / "missing.md") is None


def test_graph_status_reports_in_sync(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo", with_hooks=False)
    _seed_graph(repo, built_sha=_head(repo)[:8])

    st = graph_status(repo)
    assert st.present is True
    assert st.in_sync is True
    assert st.stale is False


def test_graph_status_reports_stale_after_a_commit(tmp_path: Path) -> None:
    """The drift the hooks cannot catch (rebase / reset --hard / a failed
    background rebuild) shows up as a SHA mismatch."""
    repo = _make_repo(tmp_path / "repo", with_hooks=False)
    out = _seed_graph(repo, built_sha=_head(repo)[:8])
    _git("commit", "-q", "--allow-empty", "-m", "moves HEAD", cwd=repo)
    # graphify has not re-checked the tree since that commit.
    os.utime(out / "manifest.json", (0, 0))

    st = graph_status(repo)
    assert st.present is True
    assert st.in_sync is False
    assert st.stamp_behind is True
    assert st.verified_current is False
    assert st.stale is True


def test_a_stamp_behind_head_is_not_stale_when_verified_since(
    tmp_path: Path,
) -> None:
    """`graphify update` re-extracts, and when topology is unchanged it prints
    "outputs left untouched" — leaving GRAPH_REPORT.md's "Built from commit"
    stamp behind forever while still refreshing manifest.json.  Reporting that
    as STALE on every run would make the check cry wolf permanently (this is
    exactly the state ~/src/vimcode was in)."""
    repo = _make_repo(tmp_path / "repo", with_hooks=False)
    out = _seed_graph(repo, built_sha=_head(repo)[:8])
    _git("commit", "-q", "--allow-empty", "-m", "moves HEAD", cwd=repo)
    # graphify re-verified against the tree AFTER that commit landed.
    os.utime(out / "manifest.json", (time.time() + 60, time.time() + 60))

    st = graph_status(repo)
    assert st.stamp_behind is True, "the stamp really is behind"
    assert st.verified_current is True
    assert st.stale is False, "verified-since means the content is current"


def test_missing_manifest_falls_back_to_the_stamp(tmp_path: Path) -> None:
    """No manifest.json → no evidence of verification → trust the stamp."""
    repo = _make_repo(tmp_path / "repo", with_hooks=False)
    _seed_graph(repo, built_sha=_head(repo)[:8], manifest=False)
    _git("commit", "-q", "--allow-empty", "-m", "moves HEAD", cwd=repo)

    st = graph_status(repo)
    assert st.verified_at is None
    assert st.verified_current is False
    assert st.stale is True


def test_graph_status_missing_graph_is_not_reported_as_stale(tmp_path: Path) -> None:
    """Absent != drifted — an unbuilt graph must not inflate the stale count."""
    repo = _make_repo(tmp_path / "repo", with_hooks=False)

    st = graph_status(repo)
    assert st.present is False
    assert st.stale is False
    assert "never built" in (st.unknown_reason or "")


def test_symlinked_worktree_freshness_is_judged_against_the_base(
    tmp_path: Path,
) -> None:
    """A worktree is on a feature branch by definition; comparing the shared
    graph against the WORKTREE's HEAD would report permanent false drift.
    Freshness must be judged against the checkout that owns the graph."""
    base = _make_repo(tmp_path / "base")
    _seed_graph(base, built_sha=_head(base)[:8])
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "feat", str(wt), cwd=base)
    # Move the worktree's HEAD away from the base's.
    _git("commit", "-q", "--allow-empty", "-m", "worktree-only commit", cwd=wt)

    st = graph_status(wt)
    assert st.is_symlink is True
    assert st.in_sync is True, "should compare against the base, not the worktree"
    assert st.stale is False


def test_unknown_freshness_is_not_counted_as_stale(tmp_path: Path) -> None:
    st = GraphStatus(repo_path=tmp_path, present=True, built_sha=None, head_sha="abc")
    assert st.stale is False


def test_hooks_path_status_flags_an_unset_hooks_path(tmp_path: Path) -> None:
    """The repo ships the bootstrap but this checkout hasn't opted in."""
    repo = _make_repo(tmp_path / "repo", with_hooks=True)
    _git("config", "--unset", "core.hooksPath", cwd=repo)
    ok, detail = hooks_path_status(repo)
    assert ok is False
    assert "core.hooksPath is unset" in detail
    assert "config core.hooksPath .githooks" in detail


def test_hooks_path_status_does_not_advise_pointing_at_a_missing_dir(
    tmp_path: Path,
) -> None:
    """A repo with no .githooks/ must NOT be told to set core.hooksPath at it —
    that would silently disable every hook for the checkout."""
    repo = _make_repo(tmp_path / "repo", with_hooks=False)
    ok, detail = hooks_path_status(repo)
    assert ok is False
    assert "no .githooks/post-checkout in this repo" in detail
    assert "config core.hooksPath" not in detail


def test_hooks_path_status_ok_when_configured(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo", with_hooks=True)
    ok, detail = hooks_path_status(repo)
    assert ok is True
    assert ".githooks" in detail


def test_hooks_path_status_flags_a_missing_hook_file(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo", with_hooks=True)
    (repo / ".githooks" / "post-checkout").unlink()
    ok, detail = hooks_path_status(repo)
    assert ok is False
    assert "missing" in detail


# ── the core.hooksPath replacement hazard ────────────────────────────────────


def test_orphaned_hooks_detects_a_locally_installed_hook_with_no_shim(
    tmp_path: Path,
) -> None:
    """core.hooksPath makes git ignore .git/hooks ENTIRELY — no merge, no
    fallback.  A graphify hook with no shim in .githooks/ stops running with
    no error and no log line.  This shipped exactly once (only post-checkout
    had a shim, silently killing the commit/merge rebuilds)."""
    from coord.graph_health import orphaned_hooks

    repo = _make_repo(tmp_path / "repo", with_hooks=True)
    # Simulate the shipped-broken state: only post-checkout has a shim.
    for name in ("post-commit", "post-merge"):
        (repo / ".githooks" / name).unlink()
    local = repo / ".git" / "hooks"
    local.mkdir(parents=True, exist_ok=True)
    for name in ("post-commit", "post-merge"):
        (local / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (local / name).chmod(0o755)

    assert orphaned_hooks(repo) == ["post-commit", "post-merge"]

    ok, detail = hooks_path_status(repo)
    assert ok is False
    assert "SILENTLY DISABLED" in detail


def test_orphaned_hooks_is_empty_once_every_hook_has_a_shim(tmp_path: Path) -> None:
    from coord.graph_health import orphaned_hooks

    # _make_repo installs the real .githooks/, which already ships shims for
    # every hook kind graphify installs.
    repo = _make_repo(tmp_path / "repo", with_hooks=True)
    local = repo / ".git" / "hooks"
    local.mkdir(parents=True, exist_ok=True)
    for name in ("post-commit", "post-merge"):
        (local / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (local / name).chmod(0o755)

    assert orphaned_hooks(repo) == []
    ok, _ = hooks_path_status(repo)
    assert ok is True


def test_orphaned_hooks_ignores_samples_and_backups(tmp_path: Path) -> None:
    """graphify leaves .pre-guard-upgrade.bak copies next to its hooks, and git
    ships .sample files — neither is ever executed."""
    from coord.graph_health import orphaned_hooks

    repo = _make_repo(tmp_path / "repo", with_hooks=True)
    local = repo / ".git" / "hooks"
    local.mkdir(parents=True, exist_ok=True)
    (local / "post-commit.pre-guard-upgrade.bak").write_text("x", encoding="utf-8")
    (local / "pre-push.sample").write_text("x", encoding="utf-8")

    assert orphaned_hooks(repo) == []


def test_every_graphify_hook_in_this_repo_has_a_versioned_shim() -> None:
    """Guard on the REAL .githooks/: adding a graphify hook kind without a shim
    silently disables it for everyone who opted into core.hooksPath."""
    versioned = HOOK_SRC.parent
    present = {p.name for p in versioned.iterdir() if p.is_file()}
    for required in ("post-checkout", "post-commit", "post-merge"):
        assert required in present, f".githooks/{required} is missing"
        assert os.access(versioned / required, os.X_OK), (
            f".githooks/{required} is not executable — git will ignore it"
        )
