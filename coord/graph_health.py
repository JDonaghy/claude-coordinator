"""Graphify knowledge-graph freshness + worktree-bootstrap health.

The repo ships a graphify graph in ``graphify-out/`` that agents are told to
query first (see CLAUDE.md).  Two things silently break it, and neither is
visible from the code:

**1. Linked worktrees are graph-blind.**  ``graphify-out/`` is gitignored by
design (only its ``.gitignore`` is tracked), so ``git worktree add`` produces
an empty one — and ``graphify query`` resolves ``graphify-out/graph.json``
strictly relative to cwd, with no upward walk and no ``--graph`` override.
``.githooks/post-checkout`` fixes this by symlinking a worktree's
``graphify-out`` at the base checkout's graph, but that hook only runs where
``core.hooksPath`` points at ``.githooks`` — a one-time, per-machine
``git config`` that nothing enforces.  :func:`hooks_path_status` checks it.

**2. The graph drifts out of sync with HEAD.**  graphify's own hooks are
best-effort and structurally cannot cover every ref-moving operation:

* ``post-commit``/``post-checkout``/``post-merge`` all ``exit 0`` during a
  rebase, merge, or cherry-pick — so the merge agent's proactive rebase
  (#306), the single most common ref move in the fleet, never rebuilds.
* ``git reset --hard`` fires no rebuild hook at all; git has none.
* Every hook failure path is ``exit 0``, and the rebuild itself is a detached
  background process with a 600s ``SIGALRM`` timeout that logs to
  ``~/.cache/graphify-rebuild.log`` — a timeout, an OOM, or an ENOENT from a
  reaped worktree all fail invisibly.
* Concurrent triggers coalesce ("Rebuild already in progress — changes
  queued").
* The hooks' own ``[ ! -f graphify-out/graph.json ] && exit 0`` guard is a
  permanent off-switch: purge the graph once and they no-op forever.

So the hooks are an optimization, not the correctness mechanism.  Correctness
comes from a cheap *check*, which is possible because ``GRAPH_REPORT.md``
records the commit it was built from::

    - Built from commit: `5be69d08`

:func:`graph_status` compares that to HEAD.  ``coord diagnose --graph``
surfaces it, so drift shows up in a routine health check instead of being
discovered by a confused agent mid-task.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# ``- Built from commit: `5be69d08` `` in GRAPH_REPORT.md.  This is the only
# machine-readable record of the graph's source commit — manifest.json holds
# per-file hashes, not a commit.
_BUILT_FROM_RE = re.compile(r"^-\s*Built from commit:\s*`([0-9a-fA-F]+)`", re.MULTILINE)

# The versioned hooks directory this repo expects core.hooksPath to point at.
HOOKS_PATH = ".githooks"


@dataclass
class GraphStatus:
    """Freshness of one checkout's graphify graph."""

    repo_path: Path
    present: bool = False
    # True when graphify-out is a symlink — i.e. a worktree borrowing the base
    # checkout's graph (the .githooks/post-checkout bootstrap ran).
    is_symlink: bool = False
    link_target: Path | None = None
    built_sha: str | None = None
    head_sha: str | None = None
    in_sync: bool = False
    age_seconds: float | None = None
    # mtime of graphify-out/manifest.json — the last time graphify *checked* the
    # graph against the working tree, whether or not it rewrote anything.
    verified_at: float | None = None
    # Commit timestamp of HEAD, to compare against verified_at.
    head_committed_at: float | None = None
    # Set when we could not determine freshness at all (no report, no git).
    unknown_reason: str | None = None

    @property
    def stamp_behind(self) -> bool:
        """``GRAPH_REPORT.md``'s "Built from commit" differs from HEAD."""
        return bool(self.present and self.built_sha and self.head_sha and not self.in_sync)

    @property
    def verified_current(self) -> bool:
        """graphify has checked the graph against the tree since HEAD landed.

        ``graphify update`` re-extracts, compares topology, and when nothing
        changed prints "No code-graph topology changes detected; outputs left
        untouched" — it deliberately does NOT rewrite ``graph.json`` or
        ``GRAPH_REPORT.md``, so the "Built from commit" stamp stays at whatever
        HEAD was the last time the content actually changed.  It *does* still
        call ``save_manifest``, so ``manifest.json``'s mtime is the honest
        record of "last verified".

        Without this, a checkout whose graph is genuinely current but whose
        stamp is behind would be reported STALE on every run, forever — a
        health check that cries wolf is worse than none.
        """
        if self.verified_at is None or self.head_committed_at is None:
            return False
        return self.verified_at >= self.head_committed_at

    @property
    def stale(self) -> bool:
        """The stamp is behind AND graphify has not verified the graph against
        the tree since HEAD landed.  Deliberately False when freshness is
        *unknown* — an unknown is reported separately, not counted as drift."""
        return self.stamp_behind and not self.verified_current


def read_built_sha(report_path: Path) -> str | None:
    """The commit ``GRAPH_REPORT.md`` says the graph was built from."""
    try:
        text = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _BUILT_FROM_RE.search(text)
    return m.group(1) if m else None


def _git_out(repo_path: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _head_sha(repo_path: Path) -> str | None:
    return _git_out(repo_path, "rev-parse", "HEAD")


def _head_committed_at(repo_path: Path) -> float | None:
    raw = _git_out(repo_path, "log", "-1", "--format=%ct", "HEAD")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _shas_agree(built: str, head: str) -> bool:
    """Compare on the shorter of the two — the report abbreviates (8 chars by
    default) while ``git rev-parse HEAD`` is full-length."""
    n = min(len(built), len(head))
    if n < 4:  # too short to be a meaningful comparison
        return False
    return built[:n].lower() == head[:n].lower()


def graph_status(repo_path: Path) -> GraphStatus:
    """Freshness of the graphify graph for the checkout at *repo_path*.

    Read-only and best-effort: a missing graph, a missing report, or a repo
    git can't read all return a populated :class:`GraphStatus` with
    ``unknown_reason`` set rather than raising.
    """
    st = GraphStatus(repo_path=repo_path)
    out_dir = repo_path / "graphify-out"

    st.is_symlink = out_dir.is_symlink()
    if st.is_symlink:
        try:
            st.link_target = out_dir.resolve()
        except OSError:
            st.link_target = None

    graph_file = out_dir / "graph.json"
    if not graph_file.is_file():
        st.unknown_reason = "no graphify-out/graph.json (graph never built here)"
        return st
    st.present = True

    try:
        st.age_seconds = max(0.0, time.time() - graph_file.stat().st_mtime)
    except OSError:
        st.age_seconds = None

    try:
        st.verified_at = (out_dir / "manifest.json").stat().st_mtime
    except OSError:
        st.verified_at = None

    st.built_sha = read_built_sha(out_dir / "GRAPH_REPORT.md")
    # Freshness is always judged against the checkout that OWNS the graph.  For
    # a symlinked worktree that's the base checkout, not the worktree's own
    # HEAD — the worktree is on a feature branch by definition and comparing
    # against it would report permanent, meaningless drift.
    owner = st.link_target.parent if (st.is_symlink and st.link_target) else repo_path
    st.head_sha = _head_sha(owner)
    st.head_committed_at = _head_committed_at(owner)

    if not st.built_sha:
        st.unknown_reason = "GRAPH_REPORT.md has no 'Built from commit' line"
    elif not st.head_sha:
        st.unknown_reason = f"could not read HEAD of {owner}"
    else:
        st.in_sync = _shas_agree(st.built_sha, st.head_sha)
    return st


def hooks_path_status(repo_path: Path) -> tuple[bool, str]:
    """``(ok, detail)`` for this checkout's ``core.hooksPath``.

    The versioned ``.githooks/post-checkout`` bootstrap only runs when
    ``core.hooksPath`` points at it — a one-time per-machine ``git config``.
    Without it, worktrees on this machine stay graph-blind and nothing says so.
    """
    try:
        r = subprocess.run(
            ["git", "config", "--get", "core.hooksPath"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"could not read core.hooksPath ({exc})"
    value = r.stdout.strip()
    have_hook = (repo_path / HOOKS_PATH / "post-checkout").is_file()
    if not value:
        if not have_hook:
            # The repo doesn't ship the bootstrap at all — telling the operator
            # to point core.hooksPath at a directory that isn't there would
            # silently disable ALL hooks for that checkout.
            return False, (
                f"no {HOOKS_PATH}/post-checkout in this repo — worktrees here get "
                f"no linked graph (port the hook to this repo to enable it)"
            )
        return False, (
            f"core.hooksPath is unset — worktrees on this machine will NOT get "
            f"a linked graph. Fix: git -C {repo_path} config core.hooksPath {HOOKS_PATH}"
        )
    if Path(value).name != Path(HOOKS_PATH).name:
        return False, (
            f"core.hooksPath is {value!r}, expected {HOOKS_PATH!r} — the worktree "
            f"graph bootstrap will not run"
        )
    if not have_hook:
        return False, (
            f"core.hooksPath={value} but {HOOKS_PATH}/post-checkout is missing "
            f"(stale checkout?)"
        )
    return True, f"core.hooksPath={value}"


def format_status_lines(st: GraphStatus) -> list[str]:
    """Human-readable report lines for *st* (used by ``coord diagnose --graph``)."""
    lines: list[str] = []
    where = str(st.repo_path)
    if not st.present:
        lines.append(f"✗ {where}: {st.unknown_reason}")
        return lines

    if st.is_symlink:
        lines.append(f"↳ {where}/graphify-out → {st.link_target} (linked worktree)")

    if st.unknown_reason:
        lines.append(f"? {where}: freshness unknown — {st.unknown_reason}")
    elif st.in_sync:
        lines.append(f"✓ {where}: graph in sync (built from {st.built_sha})")
    elif st.verified_current:
        # Stamp behind, but graphify has re-checked the tree since HEAD landed
        # and found no topology change — the graph content is current.
        lines.append(
            f"✓ {where}: graph content current — stamp says {st.built_sha} "
            f"(HEAD {(st.head_sha or '')[:8]}), but verified against the tree "
            f"since; graphify leaves outputs untouched when topology is unchanged"
        )
    else:
        lines.append(
            f"⚠ {where}: graph is STALE — built from {st.built_sha}, "
            f"HEAD is {(st.head_sha or '')[:8]}"
        )
    if st.age_seconds is not None:
        lines.append(f"    graph.json age: {st.age_seconds / 3600.0:.1f}h")
    return lines
